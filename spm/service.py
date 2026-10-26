"""Glue between the dataset, models, EV maths and reasoning; returns JSON-ready dicts."""

import datetime as dt
import statistics

import numpy as np

from . import advanced, backtest, ev, features, lineups, ml, model, props, reasoning
from .data import FIRST_SEASON, TEAMS, norm_name, season_label

SEASON_STATS = ("PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV", "PRA")
DISAGREE = 0.12  # ML vs formula gap that marks a projection as low-confidence
BIAS_MIN_PLAYERS = 8  # need this many players on a stat before estimating the model-vs-market gap


def _r(x, d=1):
    return None if x is None else round(float(x), d)


class Engine:
    """Everything needed to project a player: data, the ML model (if trained) and live features."""

    def __init__(self, ds, log=print, date=None, injuries=None):
        """date (YYYY-MM-DD): score games on that day instead of today (backfills).
        injuries: a fixed injury report instead of ESPN's live one ({} = nobody listed)."""
        self.ds = ds
        self.date, self._injuries = date, injuries
        self.predictor = ml.load_predictor(log)
        self.store = None
        if self.predictor:
            log("Loading shot-zone and play-type profiles ...")
            prof = advanced.load_profiles(FIRST_SEASON, ds.current_start, log)
            self.store = features.FeatureStore(ds, prof, build_training=False)
            log("ML model loaded.")

    @property
    def mode(self):
        return "ml" if self.predictor else "formula"

    def injuries(self):
        """Latest injury report keyed by player id (ESPN, refreshed every 15 minutes)."""
        if self._injuries is not None:
            return self._injuries
        return lineups.fetch_injuries(self.ds, log=lambda *a: None)

    def _lineup_info(self, player, opponent, info, inj):
        name = lambda pid: self.ds.players[pid].name
        def missing(team_summary):
            if not team_summary:
                return []
            return [{"name": name(p), "status": inj.get(p, {}).get("status", "Out")} for p in team_summary["missing"]]
        quest = [{"name": i["name"], "status": i["status"], "team": self.ds.players[pid].team}
                 for pid, i in inj.items()
                 if not i["out"] and pid != player.id and self.ds.players[pid].team in (player.team, opponent)
                 and pid in self.store.walks and self.ds.players[pid].on_roster]
        own, opp = info.get("own_team"), info.get("opp_team")
        return {"own_missing": missing(own), "opp_missing": missing(opp), "questionable": quest[:6],
                "own_missing_pts": own["missing_pts"] if own else 0.0,
                "top_out": bool(info["lineup"].get("tm_top_out")),
                "opp_missing_def": opp["missing_def"] if opp else 0.0,
                "self_status": inj.get(player.id)}

    def project(self, player, stat, opponent=None, home=None, b2b=False):
        """Projection dict (mean/var + context) and the calibration to apply, or (None, hist)."""
        hist = model.History(player, target=self.ds.current_start)
        proj = model.project(self.ds, hist, stat, opponent=opponent, home=home, b2b=b2b)
        if proj is None:
            return None, hist, None
        cal = getattr(self.ds, "calibration", None)
        if self.predictor:
            inj = self.injuries()
            live = self.store.live(player.id, opponent, home, b2b, date=self.date, injuries=inj)
            if live:
                X, info = live
                i = features.KEYS.index(stat)
                mean = float(self.predictor.predict(stat, X[stat])[0])
                var = max(mean * float(info["disp"][i]) * info["infl"], mean + 1e-6)
                season = self.ds.current_start
                prof = self.store.prof
                proj = {**proj, "mean": mean, "var": var, "sd": var ** 0.5, "ml": {
                    "formula_mean": proj["mean"],
                    "drivers": self.predictor.drivers(stat, X[stat]),
                    "zone": prof.describe_zone_matchup(player.id, opponent, season) if opponent else None,
                    "playtype": prof.describe_playtype_matchup(player.id, opponent, season) if opponent else None,
                    "lineup": self._lineup_info(player, opponent, info, inj),
                }}
                cal = self.predictor.calibration(stat)
        return proj, hist, cal


def calibrate(proj, cal):
    """Recalibrate P(over) with a Platt fit. Fit on half-point lines, so applied to P(over | no push)."""
    live = 1 - proj["p_push"]
    if not cal or live <= 0:
        return proj
    cond = backtest.apply_platt(proj["p_over"] / live, cal["a"], cal["b"])
    return {**proj, "p_over_raw": proj["p_over"], "p_over": cond * live, "p_under": (1 - cond) * live}


# --------------------------------------------------------------------------- props

def _group_key(prop, player):
    who = player.id if player else norm_name(prop["player"])
    return f"{who}|{prop['stat']}|{prop['game_date']}|{prop['opponent']}"


def _book_allowed(book, books):
    return not books or not book or book.lower() in books


def offers_to_props(ds, rows, b2b_teams=()):
    """Collected offers (offers_current rows) -> props, resolving each player's team and the matchup.

    Parallel to rows: None where a row can't be a prop. b2b_teams: teams that played the day
    before (the model can't see that from the line alone).
    """
    out = []
    for r in rows:
        p = ds.find_player(r["player_name"])
        team = (p.team if p else None) or r["team"]
        home = team == r["home_team"] if team in (r["home_team"], r["away_team"]) else None
        try:
            out.append(props.clean({
                "player": p.name if p else r["player_name"], "stat": r["market"], "line": r["line"],
                "over_odds": r["over_odds"], "under_odds": r["under_odds"],
                "opponent": (r["away_team"] if home else r["home_team"]) if home is not None else "",
                "home": home, "book": r["book"], "source": r["source"], "b2b": team in b2b_teams,
                "game_date": (r["start_time"] - dt.timedelta(hours=5)).date().isoformat(),  # US Eastern date
            }))
        except ValueError:
            out.append(None)
    return out


def evaluate_board(engine, props, settings=None):
    """Group offers for the same player/stat/game across books and evaluate every one.

    Two passes: project every group once, then (if settings["bias_adjust"], the
    default) measure tonight's model-vs-market gap per stat (median of projection
    minus median line) and remove it before pricing, so what's left are
    player-specific disagreements rather than the model's overall lean.
    """
    settings = settings or {}
    books = {b.lower() for b in settings.get("books") or []}
    ds = engine.ds
    groups = {}
    for prop in props:
        player = ds.find_player(prop["player"])
        groups.setdefault(_group_key(prop, player), (player, []))[1].append(prop)
    projected = {}
    for key, (player, offers) in groups.items():
        if player:
            first = offers[0]
            projected[key] = engine.project(player, first["stat"], first["opponent"] or None,
                                            first["home"], first["b2b"])
    shifts = market_bias(groups, projected) if settings.get("bias_adjust", True) else {}
    return [_evaluate_group(engine, player, offers, settings, books, projected.get(key),
                            shifts.get(offers[0]["stat"]))
            for key, (player, offers) in groups.items()]


def market_bias(groups, projected):
    """{stat: {"shift", "players"}}: median (projection - median line) per stat, if enough players."""
    gaps = {}
    for key, (player, offers) in groups.items():
        proj = (projected.get(key) or (None,))[0]
        if proj:
            line = statistics.median(o["line"] for o in offers)
            gaps.setdefault(offers[0]["stat"], []).append(proj["mean"] - line)
    return {stat: {"shift": statistics.median(g), "players": len(g)}
            for stat, g in gaps.items() if len(g) >= BIAS_MIN_PLAYERS}


def evaluate_prop(engine, prop, settings=None):
    """Single offer (used by the add-prop preview)."""
    return _evaluate_group(engine, engine.ds.find_player(prop["player"]), [prop], settings or {}, set())


def _evaluate_group(engine, player, offers, settings, books, projected=None, bias=None):
    first = offers[0]
    out = {"id": first["id"], "prop": first, "ok": False,
           "offers": [{"prop": o} for o in offers]}
    if not player:
        out["error"] = f"Player '{first['player']}' not found in NBA data."
        return out
    out["player"] = {"id": player.id, "name": player.name, "team": player.team}
    proj, hist, cal = projected or engine.project(player, first["stat"], first["opponent"] or None,
                                                  first["home"], first["b2b"])
    if proj is None:
        out["error"] = (f"Only {hist.n} NBA games on record; not enough history to model "
                        f"(rookies are skipped until they log {model.MIN_GAMES} games).")
        return out

    raw_mean = proj["mean"]
    if bias and bias["shift"]:
        mean = max(0.05, raw_mean - bias["shift"])
        proj = {**proj, "mean": mean, "var": proj["var"] * mean / raw_mean,
                "sd": (proj["var"] * mean / raw_mean) ** 0.5}
    disagree = proj.get("ml") and proj["ml"]["formula_mean"] > 0 and         abs(proj["mean"] / proj["ml"]["formula_mean"] - 1) > DISAGREE
    low_conf_base = proj["team_change"] or hist.n < 25 or bool(disagree)
    best = None
    for o in out["offers"]:
        p = o["prop"]
        pl = calibrate(model.with_line(proj, p["line"]), cal)
        ctx = reasoning.player_context(hist, p["stat"], p["line"])
        low = bool(low_conf_base or (ctx and ctx["season_n"] < 15))
        res = ev.evaluate(pl, p["over_odds"], p["under_odds"], settings, low_confidence=low)
        o.update({"ev": res, "p_over": pl["p_over"], "p_under": pl["p_under"], "p_push": pl["p_push"],
                  "eligible": _book_allowed(p["book"], books), "_proj": pl, "_ctx": ctx})
        if o["eligible"] and (best is None or res["best_ev"] > best["ev"]["best_ev"]):
            best = o
    if best is None:
        best = max(out["offers"], key=lambda o: o["ev"]["best_ev"])
    pl, ctx = best.pop("_proj"), best.pop("_ctx")
    status = (pl.get("ml") or {}).get("lineup", {}).get("self_status")
    if status and status["out"]:
        for o in out["offers"]:
            o["ev"] = {**o["ev"], "decision": "PASS", "tier": None}
    for o in out["offers"]:
        o.pop("_proj", None)
        o.pop("_ctx", None)
    iv = model.interval(pl)
    out.update({
        "ok": True, "mode": engine.mode, "prop": best["prop"], "best_offer": best["prop"]["id"],
        "projection": {
            "mean": _r(pl["mean"], 2), "sd": _r(pl["sd"], 2), "minutes": _r(pl["minutes"]),
            "p_over": pl["p_over"], "p_under": pl["p_under"], "p_push": pl["p_push"],
            "low": iv[0], "high": iv[1], "label": pl["label"], "n_games": pl["n_games"],
            "formula_mean": _r(pl["ml"]["formula_mean"], 2) if pl.get("ml") else None,
            "raw_mean": _r(raw_mean, 2), "bias_shift": _r(bias["shift"], 2) if bias else None,
        },
        "ev": best["ev"],
        "reasoning": reasoning.prop_reasoning(pl, ctx, best["ev"], player, iv),
        "recent": ctx["recent"] if ctx else [],
    })
    if bias and bias["shift"]:
        out["reasoning"]["points"].insert(1, {
            "lean": "neutral",
            "text": f"Market-bias adjustment: across {bias['players']} players tonight the model runs "
                    f"{abs(bias['shift']):.1f} {'below' if bias['shift'] < 0 else 'above'} the books on {first['stat']}, "
                    f"so {abs(bias['shift']):.1f} was {'added to' if bias['shift'] < 0 else 'taken off'} this projection "
                    f"(raw {raw_mean:.1f}). What's left is how this player differs from the market, "
                    f"not the model's overall lean.",
        })
    if disagree:
        out["reasoning"]["points"].insert(1, {
            "lean": "neutral",
            "text": f"Caution: the ML model ({pl['mean']:.1f}) and the formula model ({pl['ml']['formula_mean']:.1f}) "
                    f"disagree by {abs(pl['mean'] / pl['ml']['formula_mean'] - 1):.0%}. Treated as low-confidence, "
                    f"so a higher EV is required.",
        })
    if len(offers) > 1:
        out["reasoning"]["points"].append({
            "lean": "neutral",
            "text": f"Best price across {len(offers)} books: {best['prop']['book'] or 'manual'} "
                    f"({best['ev']['decision'].lower() if best['ev']['decision'] != 'PASS' else best['ev']['best_side'].lower()} "
                    f"{best['prop']['line']} at {best['ev']['sides'][best['ev']['best_side']]['odds']:+d}).",
        })
    return out


# --------------------------------------------------------------------------- season view

def season_projections(engine):
    ds = engine.ds
    rows, ml_rows = [], []
    for p in ds.players.values():
        if not p.on_roster:
            continue
        hist = model.History(p, target=ds.current_start)
        if hist.n < model.MIN_GAMES:
            rows.append({"id": p.id, "name": p.name, "team": p.team, "games": hist.n, "rookie": True})
            continue
        row = {"id": p.id, "name": p.name, "team": p.team, "games": hist.n,
               "min": _r(hist.min_base), "changed_team": p.changed_team, "prev_team": p.last_team}
        for s in SEASON_STATS:
            row[s] = _r(model.project(ds, hist, s)["mean"])
        rows.append(row)
        if engine.predictor:
            typical = engine.predictor.report.get("typical") or {"prior_share": 0.25}
            home = engine.store.live(p.id, None, True, typical=typical)
            away = engine.store.live(p.id, None, False, typical=typical)
            if home and away:
                ml_rows.append((row, home[0], away[0]))
    if ml_rows:
        # Neutral opponent, averaged over home and road; one batched predict per stat.
        for s in SEASON_STATS:
            Xh = np.vstack([h[s] for _, h, _ in ml_rows])
            Xa = np.vstack([a[s] for _, _, a in ml_rows])
            pred = (engine.predictor.predict(s, Xh) + engine.predictor.predict(s, Xa)) / 2
            for (row, _, _), v in zip(ml_rows, pred):
                row[s] = _r(v)
    rows.sort(key=lambda r: -(r.get("PTS") or -1))
    return rows


def player_detail(engine, pid, season_row=None):
    ds = engine.ds
    p = ds.players.get(pid)
    if not p:
        return None
    hist = model.History(p, target=ds.current_start)
    stats = []
    for s in SEASON_STATS:
        proj = model.project(ds, hist, s)
        if not proj:
            continue
        ctx = reasoning.player_context(hist, s)
        why = reasoning.season_reasoning(proj, ctx, p)
        mean, var = proj["mean"], proj["var"]
        if season_row and season_row.get(s) is not None and engine.predictor:
            mean = season_row[s]
            var = max(mean * proj["dispersion"] * proj["inflation"], mean + 1e-6)
            why.insert(0, f"ML model projects {mean:.1f} (formula model {proj['mean']:.1f}), "
                          f"for a typical mid-season game: average opponent, normal rest, the usual number of "
                          f"players missing on both sides, averaged over home and road.")
        lo, hi = model.quantile(mean, var, 0.1), model.quantile(mean, var, 0.9)
        stats.append({"stat": s, "label": proj["label"], "mean": _r(mean), "low": lo, "high": hi, "reasoning": why})
    by_season = {}
    for g in p.games:
        b = by_season.setdefault(g.season, {"gp": 0, "min": 0, "pts": 0, "reb": 0, "ast": 0, "fg3m": 0, "teams": []})
        b["gp"] += 1
        for f in ("min", "pts", "reb", "ast", "fg3m"):
            b[f] += getattr(g, f)
        if g.team not in b["teams"]:
            b["teams"].append(g.team)
    seasons = [{"season": season_label(s), "gp": b["gp"], "team": "/".join(b["teams"]),
                **{f: _r(b[f] / b["gp"]) for f in ("min", "pts", "reb", "ast", "fg3m")}}
               for s, b in sorted(by_season.items(), reverse=True)]
    return {"id": p.id, "name": p.name, "team": p.team, "team_name": TEAMS.get(p.team, p.team),
            "prev_team": p.last_team, "changed_team": p.changed_team,
            "experience": (ds.current_start - p.from_year) if p.from_year else None,
            "projected_minutes": _r(hist.min_base), "games": hist.n,
            "stats": stats, "seasons": seasons}
