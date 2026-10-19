"""Walk-forward backtest on the most recent completed season.

For each sampled game, the model only sees games played before that date and the
previous season's defensive numbers. Historical sportsbook lines aren't freely
available, so each game's "line" is the player's season-to-date average rounded
to x.5 at -110/-110, a reasonable stand-in for how books set lines.
"""

import datetime as dt
import json
import math
import os
import time
import zlib

from . import ev, model
from .data import CACHE_DIR, season_label

STATS = ("PTS", "REB", "AST", "3PM", "PRA")
SAMPLE_EVERY = 3
MIN_PROJ_MINUTES = 20


def _cache_path(ds):
    return os.path.join(CACHE_DIR, f"backtest_{int(ds.meta['built'])}.json")


def cached(ds):
    for name in sorted(os.listdir(CACHE_DIR)) if os.path.isdir(CACHE_DIR) else []:
        if name.startswith("backtest_"):
            with open(os.path.join(CACHE_DIR, name), encoding="utf-8") as f:
                return json.load(f)
    return None


def run(ds, progress=lambda frac: None):
    season = max(s for s in ds.league_avg if s < ds.current_start)
    start_date = f"{season + 1}-01-01"
    records = {s: [] for s in STATS}
    players = [p for p in ds.players.values() if any(g.season == season for g in p.games)]

    for i, p in enumerate(players):
        if i % 25 == 0:
            progress(i / len(players))
        prev_date = None
        season_vals = {s: [] for s in STATS}
        for g in p.games:
            if g.season != season:
                continue
            fields_vals = {s: model.stat_value(g, model.STATS[s][1]) for s in STATS}
            b2b = prev_date is not None and (dt.date.fromisoformat(g.date) - dt.date.fromisoformat(prev_date)).days == 1
            sampled = (g.date >= start_date and g.min >= 1
                       and zlib.crc32(f"{p.id}{g.game_id}".encode()) % SAMPLE_EVERY == 0
                       and len(season_vals["PTS"]) >= 10)
            if sampled:
                hist = model.History(p, asof=g.date, target=season)
                if hist.n >= model.MIN_GAMES and hist.min_base >= MIN_PROJ_MINUTES:
                    for s in STATS:
                        prior = season_vals[s]
                        naive = sum(prior) / len(prior)
                        line = int(naive) + 0.5
                        proj = model.project(ds, hist, s, opponent=g.opp, home=g.home, b2b=b2b,
                                             asof=g.date, live=False)
                        proj = model.with_line(proj, line)
                        res = ev.evaluate(proj, -110, -110)
                        records[s].append({
                            "mean": proj["mean"], "naive": naive,
                            "l10": sum(prior[-10:]) / len(prior[-10:]),
                            "p_over": proj["p_over"], "actual": fields_vals[s], "line": line,
                            "decision": res["decision"], "ev": res["best_ev"],
                        })
            if g.min >= 1:
                for s in STATS:
                    season_vals[s].append(fields_vals[s])
            prev_date = g.date

    result = {"season": season_label(season), "start_date": start_date,
              "generated": time.time(), "stats": {}, "calibration": []}
    all_recs = []
    for s, recs in records.items():
        if not recs:
            continue
        all_recs += recs
        result["stats"][s] = _summarise(recs)
    result["platt"] = fit_platt([(r["p_over"], r["actual"] > r["line"]) for r in all_recs])
    result["overall"] = _summarise(all_recs)
    result["calibration"] = _calibration(all_recs)
    # How the recalibrated probabilities score on the same games (in-sample, 2 parameters).
    a, b = result["platt"]["a"], result["platt"]["b"]
    for r in all_recs:
        r["p_over_raw"], r["p_over"] = r["p_over"], apply_platt(r["p_over"], a, b)
    result["calibration_after"] = _calibration(all_recs)
    result["brier_after"] = sum((r["p_over"] - (r["actual"] > r["line"])) ** 2 for r in all_recs) / len(all_recs)

    os.makedirs(CACHE_DIR, exist_ok=True)
    for name in os.listdir(CACHE_DIR):
        if name.startswith("backtest_"):
            os.remove(os.path.join(CACHE_DIR, name))
    with open(_cache_path(ds), "w", encoding="utf-8") as f:
        json.dump(result, f)
    progress(1.0)
    return result


def _summarise(recs):
    n = len(recs)
    mae = lambda key: sum(abs(r[key] - r["actual"]) for r in recs) / n
    brier = sum((r["p_over"] - (r["actual"] > r["line"])) ** 2 for r in recs) / n
    over_rate = sum(r["actual"] > r["line"] for r in recs) / n
    bets = [r for r in recs if r["decision"] != "PASS"]
    wins = sum(1 for r in bets if (r["actual"] > r["line"]) == (r["decision"] == "OVER"))
    profit = wins * (100 / 110) - (len(bets) - wins)
    return {
        "n": n, "mae_model": mae("mean"), "mae_season_avg": mae("naive"), "mae_last10": mae("l10"),
        "brier": brier, "brier_baseline": over_rate * (1 - over_rate),
        "bets": len(bets), "bet_win_rate": wins / len(bets) if bets else None,
        "roi": profit / len(bets) if bets else None,
    }


def _calibration(recs):
    bins = []
    for lo in range(0, 100, 10):
        sel = [r for r in recs if lo / 100 <= r["p_over"] < (lo + 10) / 100]
        if len(sel) < 20:
            continue
        bins.append({"bin": f"{lo}-{lo + 10}%", "n": len(sel),
                     "predicted": sum(r["p_over"] for r in sel) / len(sel),
                     "actual": sum(r["actual"] > r["line"] for r in sel) / len(sel)})
    return bins


def _logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def apply_platt(p, a, b):
    return 1 / (1 + math.exp(-(a + b * _logit(p))))


def fit_platt(pairs, iters=50):
    """Fit logit(p_true) = a + b * logit(p_model) by Newton's method on log-loss."""
    a, b = 0.0, 1.0
    xs = [(_logit(p), 1.0 if y else 0.0) for p, y in pairs]
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in xs:
            q = 1 / (1 + math.exp(-(a + b * x)))
            d, w = q - y, q * (1 - q)
            ga += d
            gb += d * x
            haa += w
            hab += w * x
            hbb += w * x * x
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return {"a": a, "b": b, "n": len(xs)}
