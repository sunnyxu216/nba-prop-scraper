"""Score live NFL lines: projection, probabilities, EV, a decision and the reasoning, per offer.

    from spm.nfl import live
    scorer = live.Scorer()                 # loads models + history (~30 s)
    results = scorer.score(rows)           # rows: pg.current_lines(conn, sport="nfl")

EV and the decision come from spm/ev.py, the same code as the NBA board: model and no-vig
market probabilities are blended, and a bet needs EV above the threshold (higher for
low-confidence spots).
"""

import datetime as dt
import math

from .. import ev
from ..data import norm_name
from . import data, features
from .model import LABELS, MARKETS, Predictor

ONE_SIDED_HOLD = 0.10      # assumed bookmaker margin when only a "yes" price is posted (Anytime TD)


def _fmt(x, d=1):
    return f"{x:.{d}f}" if x is not None and not math.isnan(x) else "n/a"


def one_sided_prices(over_odds):
    """A synthetic under price for a yes-only market, so the market probability can be estimated.

    The "yes" implied probability is divided by (1 + hold); the fake "no" price makes the pair sum to it.
    """
    p_yes = ev.implied_prob(over_odds) / (1 + ONE_SIDED_HOLD)
    p_no_implied = (1 - p_yes) * (1 + ONE_SIDED_HOLD)
    p_no_implied = min(p_no_implied, 0.99)
    under = -round(100 * p_no_implied / (1 - p_no_implied)) if p_no_implied >= 0.5 \
        else round(100 * (1 - p_no_implied) / p_no_implied)
    return int(under)


class Scorer:
    def __init__(self, log=print, predictor=None, built=None):
        self.predictor = predictor or Predictor()
        self.games = data.load_games()
        if built is None:
            features.set_games(self.games)
            fb = features.FeatureBuilder(data.load_participation())
            for _ in fb.walk(data.load_player_games()):
                pass
            built = fb
        self.fb = built
        self.injuries = {}
        log(f"NFL scorer ready (model {self.predictor.version})")

    def _game(self, home, away, start):
        day = (start - dt.timedelta(hours=5)).date().isoformat() if start else None
        for g in self.games.values():
            if g["home"] == home and g["away"] == away and g["home_score"] is None and \
                    (day is None or abs((dt.date.fromisoformat(g["gameday"]) - dt.date.fromisoformat(day)).days) <= 1):
                return g
        return None

    def _injuries(self, season, week):
        if (season, week) not in self.injuries:
            self.injuries[(season, week)] = data.load_injuries(season, week)
        return self.injuries[(season, week)]

    def score(self, rows, settings=None):
        """rows: dicts with player_name, team, market, book, line, over_odds, under_odds, home_team,
        away_team, start_time, source. Returns one result per row (skipped rows carry a reason)."""
        cache, out = {}, []
        for r in rows:
            res = {k: r.get(k) for k in ("source", "player_name", "team", "market", "book", "line", "over_odds",
                                          "under_odds", "home_team", "away_team", "start_time")}
            try:
                res.update(self._score_one(r, settings, cache))
            except LookupError as e:
                res.update({"decision": None, "skipped": str(e)})
            out.append(res)
        return out

    def _score_one(self, r, settings, cache):
        market = r["market"]
        if market not in MARKETS:
            raise LookupError(f"market {market} not modeled")
        g = self._game(r["home_team"], r["away_team"], r.get("start_time"))
        if g is None:
            raise LookupError("game not in the nflverse schedule")
        key = norm_name(r["player_name"])
        p = self.fb.players.get(key)
        if p is None or not p.n:
            raise LookupError("no NFL history for this player (rookie debut or name mismatch)")
        team = r.get("team") or p.team
        if team not in (g["home"], g["away"]):
            raise LookupError(f"player's team {team} not in this game")
        opp = g["away"] if team == g["home"] else g["home"]
        ck = (key, g["game_id"])
        if ck not in cache:
            ctx = data.game_context(g, team)
            cache[ck] = self.fb.live_row(key, p.position, team, opp, g["season"], g["week"], ctx,
                                         self._injuries(g["season"], g["week"]))
        feats = cache[ck]
        proj = self.predictor.with_line(self.predictor.project(market, feats), float(r["line"]))
        one_sided = r.get("under_odds") is None
        under_odds = one_sided_prices(r["over_odds"]) if one_sided else r["under_odds"]
        low_conf = (not proj["eligible"]) or feats["season_games"] < 3 or feats["games_missed"] > 0
        result = ev.evaluate(proj, r["over_odds"], under_odds, settings, low_confidence=low_conf)
        if one_sided and result["decision"] == "UNDER":     # there is no "no" bet to place
            result["decision"], result["tier"] = "PASS", None
        injury = self._injuries(g["season"], g["week"]).get((team, key))
        return {"projection": proj["mu"], "kind": proj["kind"], "p_over": proj["p_over"], "p_under": proj["p_under"],
                "p_push": proj["p_push"], "decision": result["decision"], "tier": result["tier"],
                "best_ev": result["best_ev"], "ev_over": result["sides"]["OVER"]["ev"],
                "ev_under": None if one_sided else result["sides"]["UNDER"]["ev"],
                "stake_pct": result["sides"][result["best_side"]]["kelly_pct"] if result["decision"] != "PASS" else 0.0,
                "market_prob_over": result["sides"]["OVER"]["market_prob"], "low_confidence": low_conf,
                "one_sided": one_sided, "injury_status": injury, "model_version": self.predictor.version,
                "game_id": g["game_id"], "position": p.position, "opp": opp,
                "reasoning": reasoning(market, proj, feats, result, r, opp, one_sided, injury)}


def reasoning(market, proj, f, result, offer, opp, one_sided, injury):
    """Plain-English explanation: the projection, what drives it, and the bet math."""
    label, kind = LABELS[market], MARKETS[market][1]
    cols = MARKETS[market][0]
    line = float(offer["line"])
    lines = []
    if kind == "binary":
        lines.append(f"Model: {proj['p_over']:.0%} chance to score a touchdown "
                     f"(recent TD rate {_fmt(f['any_td_l'], 2)} per game).")
    else:
        lines.append(f"Projection {proj['mu']:.1f} {label} vs a line of {line:g}: "
                     f"{proj['p_over']:.0%} over / {proj['p_under']:.0%} under.")
        recent = " + ".join(_fmt(f.get(f"{c}_s")) for c in cols)
        season = " + ".join(_fmt(f.get(f"{c}_szn")) for c in cols)
        lines.append(f"Form: last few games {recent}; season average {season} "
                     f"over {int(f['season_games'])} games.")
    role = []
    if f["pos_QB"]:
        role.append(f"{_fmt(f['pass_share_s'] * 100, 0)}% of team pass attempts")
    if not math.isnan(f["tgt_share_s"]):
        role.append(f"{_fmt(f['tgt_share_s'] * 100, 0)}% target share")
    if not math.isnan(f["carry_share_s"]) and f["carry_share_s"] > 0.05:
        role.append(f"{_fmt(f['carry_share_s'] * 100, 0)}% of carries")
    if not math.isnan(f["snap_s"]):
        role.append(f"{_fmt(f['snap_s'] * 100, 0)}% of snaps")
    if role:
        lines.append("Role: " + ", ".join(role) + ".")
    side = "home" if f["home"] else "on the road"
    fav = f"favored by {f['spread']:g}" if f["spread"] > 0 else f"a {-f['spread']:g}-point underdog" \
        if f["spread"] < 0 else "a pick'em"
    lines.append(f"Game: vs {opp} {side}, {fav}; team expected to score {_fmt(f['implied_pts'])} "
                 f"(total {_fmt(f['game_total'])}).")
    effects = sorted(((g, v) for g, v in proj["groups"].items() if g != "Recent form" and abs(v) > 1e-6),
                     key=lambda t: -abs(t[1]))[:3]
    if effects:
        unit = {"count": "%", "yards": " yds", "binary": " (log-odds)"}[kind]
        fmt = (lambda v: f"{(math.exp(v) - 1) * 100:+.0f}%") if kind == "count" else (lambda v: f"{v:+.1f}{unit}")
        lines.append("Biggest adjustments: " + "; ".join(f"{g} {fmt(v)}" for g, v in effects) + ".")
    if f["vac_tgt"] > 0.1 or f["vac_carry"] > 0.15 or f["vac_pass"] > 0.3:
        lines.append(f"Teammates out or doubtful free up {_fmt(f['vac_tgt'] * 100, 0)}% of targets and "
                     f"{_fmt(f['vac_carry'] * 100, 0)}% of carries.")
    if injury:
        lines.append(f"On the injury report: {injury}.")
    if f["games_missed"] > 0:
        lines.append(f"Returning after missing {int(f['games_missed'])} game(s).")
    s = result["sides"]
    if one_sided:
        lines.append(f"Only a 'yes' price is posted ({offer['over_odds']:+d}); the market's probability "
                     f"({s['OVER']['market_prob']:.0%}) assumes a {ONE_SIDED_HOLD:.0%} bookmaker margin.")
    if result["decision"] == "PASS":
        lines.append(f"No bet: best EV {result['best_ev'] * 100:+.1f}% is below the "
                     f"{result['threshold'] * 100:.1f}% threshold.")
    else:
        side_ = result["decision"]
        lines.append(f"{result['tier']} {side_}: blended probability {s[side_]['blended_prob']:.0%} vs "
                     f"breakeven {s[side_]['breakeven']:.0%}, EV {s[side_]['ev'] * 100:+.1f}% per $1.")
    if result.get("low_confidence"):
        lines.append("Low confidence (small sample, new role or returning from injury): higher EV bar applied.")
    return lines
