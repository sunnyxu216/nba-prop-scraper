"""The NFL board for the dashboard: scored lines grouped by player and market, with line shopping.

Rows come from predictions_latest (the cloud's hourly scoring) or a local scoring run. Their
probabilities don't depend on the user's settings, so EV and the decision are recomputed here
with whatever minimum EV, market blend and books the dashboard has selected.
"""

from .. import ev
from .live import NFL_DEFAULTS, one_sided_prices
from .model import LABELS

# reasoning lines that state a decision under the cloud's default settings; the board shows its
# own decision for the user's settings instead
_DECISION_PREFIXES = ("No bet:", "Strong ", "Lean ")


def evaluate(row, settings):
    """EV and decision for one stored line under the given settings."""
    s = {**NFL_DEFAULTS, **(settings or {})}
    proj = {"p_over": row["p_over"], "p_under": row["p_under"], "p_push": row["p_push"]}
    under = one_sided_prices(row["over_odds"]) if row["under_odds"] is None else row["under_odds"]
    res = ev.evaluate(proj, row["over_odds"], under, s, low_confidence=row["low_confidence"])
    decision, tier = res["decision"], res["tier"]
    if (row["under_odds"] is None and decision == "UNDER") or row["track_only"]:
        decision, tier = "PASS", None
    sides = {k: {"odds": v["odds"], "model_prob": v["model_prob"], "market_prob": v["market_prob"],
                 "blended_prob": v["blended_prob"], "breakeven": v["breakeven"], "ev": v["ev"],
                 "stake": v["stake"] if decision == k else 0.0} for k, v in res["sides"].items()}
    if row["under_odds"] is None:
        sides["UNDER"] = None
    return {"decision": decision, "tier": tier, "best_side": res["best_side"], "best_ev": res["best_ev"],
            "threshold": res["threshold"], "sides": sides, "model_call": res["decision"]}


def build(rows, settings=None, books=None):
    """Group rows (one per book) into props; the best price among `books` (None = all) is the pick."""
    mine = {b.lower() for b in books or []}
    groups = {}
    for r in rows:
        key = (str(r.get("event_id") or f"{r['away_team']}@{r['home_team']}"), r["player_name"], r["market"])
        groups.setdefault(key, []).append(r)
    out = []
    for (event, name, market), offers in groups.items():
        evald = [(o, evaluate(o, settings)) for o in offers]
        allowed = [(o, e) for o, e in evald if not mine or o["book"].lower() in mine]
        at_my_books = bool(allowed)
        bets = [(o, e) for o, e in allowed if e["decision"] != "PASS"]
        best_o, best_e = max(bets or allowed or evald, key=lambda t: t[1]["best_ev"])
        if not at_my_books:            # only offered at books you don't use: shown, never a pick
            best_e = {**best_e, "decision": "PASS", "tier": None,
                      "sides": {k: v and {**v, "stake": 0.0} for k, v in best_e["sides"].items()}}
        first = offers[0]
        reasoning = [ln for ln in (best_o.get("reasoning") or []) if not ln.startswith(_DECISION_PREFIXES)]
        out.append({
            "id": f"{event}|{name}|{market}", "player": name, "team": first["team"], "opp": first["opp"],
            "position": first["position"], "market": market, "label": LABELS.get(market, market),
            "kind": first["kind"], "start_time": str(first["start_time"]), "home": first["team"] == first["home_team"],
            "projection": first["projection"], "track_only": first["track_only"],
            "outside_training": first["outside_training"], "low_confidence": first["low_confidence"],
            "one_sided": first["under_odds"] is None, "injury_status": first.get("injury_status"),
            "at_my_books": at_my_books,
            "scored_at": str(first.get("scored_at", "")), "model_version": first.get("model_version"),
            "best": {"book": best_o["book"], "line": float(best_o["line"]), "over_odds": best_o["over_odds"],
                     "under_odds": best_o["under_odds"], "p_over": best_o["p_over"], "p_under": best_o["p_under"],
                     "p_push": best_o["p_push"], **best_e},
            "books": sorted(({"book": o["book"], "line": float(o["line"]), "over_odds": o["over_odds"],
                              "under_odds": o["under_odds"], "ev_over": e["sides"]["OVER"]["ev"],
                              "ev_under": e["sides"]["UNDER"]["ev"] if e["sides"]["UNDER"] else None,
                              "decision": e["decision"], "mine": not mine or o["book"].lower() in mine,
                              "best": o is best_o} for o, e in evald), key=lambda b: -max(b["ev_over"], b["ev_under"] or -9)),
            "reasoning": reasoning,
        })
    out.sort(key=lambda g: (g["best"]["decision"] == "PASS", -g["best"]["best_ev"]))
    return out


def from_scored(offers, results):
    """Local scoring output (live.Scorer.score) -> rows shaped like predictions_latest."""
    rows = []
    for o, r in zip(offers, results):
        if r.get("skipped") or r.get("decision") is None:
            continue
        rows.append({"event_id": o.get("event_id"), "player_name": o["player_name"], "market": o["market"],
                     "book": o["book"], "line": o["line"], "over_odds": o["over_odds"], "under_odds": o["under_odds"],
                     "home_team": o["home_team"], "away_team": o["away_team"], "start_time": o["start_time"],
                     "team": r["team"], "opp": r["opp"], "position": r["position"], "kind": r["kind"],
                     "projection": r["projection"], "p_over": r["p_over"], "p_under": r["p_under"],
                     "p_push": r["p_push"], "track_only": r["track_only"], "outside_training": r["outside_training"],
                     "low_confidence": r["low_confidence"], "injury_status": r["injury_status"],
                     "model_version": r["model_version"], "reasoning": r["reasoning"]})
    return rows
