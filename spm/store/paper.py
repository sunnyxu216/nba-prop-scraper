"""Paper trading: log what the model said about each line, then measure it against results.

record(): called by scripts/score_nfl.py each hourly run.
summary(): ROI, closing-line value (CLV) and calibration, from predictions joined to grades.

Each pick is settled at its own line and price against the actual result (grades.actual), so a
pick made at 4.5 is graded at 4.5 even if the line closed at 5.5.

Closing-line value (CLV): how far the market moved toward the pick between the pick and the close,
at the same book and line, in no-vig probability points (fair p of the picked side at close minus
at the pick). It is ~0 at the closing checkpoint by construction; consistently positive CLV at
earlier checkpoints is the earliest sign of real edge (it needs far fewer picks than ROI). When the
line itself moved, probabilities aren't comparable, so the direction of the move is counted instead
(line_moved_for / line_moved_against). Yes-only prices have no fair probability: no CLV.
"""

import datetime as dt
import json

from .. import ev

CHECKPOINTS = ("open", "t24", "t2", "close")


def checkpoints(start, now):
    """Which checkpoints a scoring run at `now` fills for a game starting at `start`."""
    hours = (start - now).total_seconds() / 3600
    out = ["open"]
    if 2.5 < hours <= 24:
        out.append("t24")
    elif 1.0 < hours <= 2.5:
        out.append("t2")
    elif 0 < hours <= 1.0:
        out.append("close")
    return out if hours > 0 else []


def open_offers(conn, sport="nfl", max_age_hours=2):
    """Lines to score: seen by the collector recently, for games not yet started."""
    with conn.cursor() as cur:
        cur.execute("""
            select oc.event_id, oc.source, oc.player_key, oc.player_name, oc.team, oc.market, oc.book,
                   oc.line::float, oc.over_odds, oc.under_odds, oc.last_seen,
                   e.home_team, e.away_team, e.start_time
            from offers_current oc join events e on e.id = oc.event_id
            where oc.sport = %s and e.start_time > now()
              and oc.last_seen > now() - make_interval(hours => %s)""", (sport, max_age_hours))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


PRED_COLS = ["event_id", "sport", "source", "player_key", "player_name", "market", "book", "checkpoint",
             "scored_at", "price_seen_at", "line", "over_odds", "under_odds", "model_version", "projection",
             "p_over", "p_under", "p_push", "market_prob_over", "ev_over", "ev_under", "decision", "model_call",
             "tier", "stake_pct", "track_only", "low_confidence", "reasoning"]
LATEST_COLS = ["event_id", "sport", "source", "player_key", "player_name", "market", "book", "scored_at",
               "start_time", "home_team", "away_team", "team", "opp", "position", "line", "over_odds",
               "under_odds", "model_version", "kind", "projection", "p_over", "p_under", "p_push", "decision",
               "model_call", "track_only", "outside_training", "low_confidence", "one_sided", "injury_status",
               "reasoning"]


def record(conn, offers, results, sport="nfl", now=None):
    """Store one scoring run. offers and results are parallel lists (live.Scorer.score output).

    Returns (checkpoint rows written, latest rows written).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    log_rows, latest = [], []
    for o, r in zip(offers, results):
        if r.get("skipped") or r.get("decision") is None:
            continue
        would_bet = r["model_call"] != "PASS"
        base = {"event_id": o["event_id"], "sport": sport, "source": o["source"], "player_key": o["player_key"],
                "player_name": o["player_name"], "market": o["market"], "book": o["book"], "scored_at": now,
                "line": o["line"], "over_odds": o["over_odds"], "under_odds": o["under_odds"],
                "model_version": r["model_version"], "projection": r["projection"], "p_over": r["p_over"],
                "p_under": r["p_under"], "p_push": r["p_push"], "decision": r["decision"],
                "model_call": r["model_call"], "track_only": r["track_only"], "low_confidence": r["low_confidence"]}
        for cp in checkpoints(o["start_time"], now):
            log_rows.append({**base, "checkpoint": cp, "price_seen_at": o["last_seen"],
                             "market_prob_over": r["market_prob_over"], "ev_over": r["ev_over"],
                             "ev_under": r["ev_under"], "tier": r["tier"], "stake_pct": r["stake_pct"],
                             "reasoning": json.dumps(r["reasoning"]) if would_bet else None})
        latest.append({**base, "start_time": o["start_time"], "home_team": o["home_team"],
                       "away_team": o["away_team"], "team": r.get("team") or o["team"] or "", "opp": r["opp"],
                       "position": r["position"], "kind": r["kind"], "outside_training": r["outside_training"],
                       "one_sided": r["one_sided"], "injury_status": r["injury_status"],
                       "reasoning": json.dumps(r["reasoning"])})
    with conn.transaction(), conn.cursor() as cur:
        for row in log_rows:
            update = row["checkpoint"] == "close"   # close = the last run before kickoff
            cur.execute(
                f"insert into predictions ({', '.join(PRED_COLS)}) values ({', '.join(['%s'] * len(PRED_COLS))}) "
                "on conflict (event_id, player_key, market, book, checkpoint) do "
                + ("update set " + ", ".join(f"{c} = excluded.{c}" for c in PRED_COLS[8:]) if update else "nothing"),
                [row[c] for c in PRED_COLS])
        cur.executemany(
            f"insert into predictions_latest ({', '.join(LATEST_COLS)}) values ({', '.join(['%s'] * len(LATEST_COLS))}) "
            "on conflict (event_id, player_key, market, book) do update set "
            + ", ".join(f"{c} = excluded.{c}" for c in LATEST_COLS[4:]), [[row[c] for c in LATEST_COLS] for row in latest])
        cur.execute("delete from predictions_latest where sport = %s and start_time < %s",
                    (sport, now - dt.timedelta(days=1)))
    return len(log_rows), len(latest)


def latest(conn, sport="nfl"):
    """The dashboard's board: latest scoring of every line for upcoming games."""
    with conn.cursor() as cur:
        cur.execute(f"""select {', '.join(LATEST_COLS)} from predictions_latest
                        where sport = %s and start_time > now() order by start_time, player_name, market, book""",
                    (sport,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- results

def _settle(side, line, actual):
    if actual is None:
        return "void"
    if actual == line:
        return "push"
    won = actual > line if side == "OVER" else actual < line
    return "win" if won else "loss"


def _clv(side, pick_over, pick_under, close_over, close_under):
    """No-vig probability of the picked side at close minus at the pick (same line), or None when
    either price is one-sided."""
    if pick_under is None or close_under is None:
        return None
    k = 0 if side == "OVER" else 1
    return ev.no_vig(close_over, close_under)[k] - ev.no_vig(pick_over, pick_under)[k]


def _line_move(side, pick_line, close_line):
    """+1 if the line moved toward the pick (an over taken at 4.5 that closed 5.5), -1 if away, 0 if not."""
    if close_line == pick_line:
        return 0
    toward = close_line > pick_line if side == "OVER" else close_line < pick_line
    return 1 if toward else -1


def graded_picks(conn, sport="nfl", checkpoint="close", include_track_only=False):
    """Every pick at one checkpoint whose game is graded, settled at its own line and price."""
    with conn.cursor() as cur:
        cur.execute("""
            select p.player_name, p.market, p.book, p.line::float, p.over_odds, p.under_odds, p.decision,
                   p.model_call, p.track_only, p.p_over, p.p_under, p.p_push, p.market_prob_over,
                   g.actual::float, g.result, g.line::float, g.over_odds, g.under_odds, p.scored_at,
                   p.event_id, p.player_key, p.ev_over, p.ev_under
            from predictions p
            join grades g on g.event_id = p.event_id and g.player_key = p.player_key
                         and g.market = p.market and g.book = p.book
            where p.sport = %s and p.checkpoint = %s""", (sport, checkpoint))
        out = []
        for (name, market, book, line, over, under, decision, call, track, p_over, p_under, p_push, mkt,
             actual, result, close_line, close_over, close_under, scored_at, event_id, player_key,
             ev_over, ev_under) in cur.fetchall():
            side = call if include_track_only else decision
            if side == "UNDER" and under is None:   # yes-only market: a "no" call has no price to bet at
                side = "PASS"
            row = {"player_name": name, "market": market, "book": book, "line": line, "side": side,
                   "track_only": track, "p_over": p_over, "p_under": p_under, "p_push": p_push,
                   "market_prob_over": mkt, "actual": actual, "void": result == "void", "scored_at": scored_at,
                   "event_id": event_id, "player_key": player_key,
                   "ev": ev_over if side == "OVER" else ev_under if side == "UNDER" else None}
            if side in ("OVER", "UNDER"):
                odds = over if side == "OVER" else under
                outcome = _settle(side, line, actual)
                row.update(odds=odds, outcome=outcome,
                           profit={"win": ev.profit_per_unit(odds), "loss": -1.0}.get(outcome, 0.0),
                           clv=_clv(side, over, under, close_over, close_under) if close_line == line else None,
                           line_move=_line_move(side, line, close_line))
            out.append(row)
        return out


def one_per_bet(rows):
    """The same call at several books is one bet, not several: keep the book with the best EV
    (the one you'd actually bet) for each player, market and game."""
    best = {}
    for r in rows:
        key = (r["event_id"], r["player_key"], r["market"])
        if key not in best or (r["ev"] or 0) > (best[key]["ev"] or 0):
            best[key] = r
    return list(best.values())


def summary(conn, sport="nfl"):
    """Per checkpoint: picks, record, units won (1u flat), ROI, average CLV; plus model calibration.

    Each pick counts once (best book); calibration uses every scored line."""
    out = {"checkpoints": {}, "calibration": [], "track_only": None}
    for cp in CHECKPOINTS:
        rows = [r for r in graded_picks(conn, sport, cp) if r["side"] in ("OVER", "UNDER")]
        out["checkpoints"][cp] = _record(one_per_bet(rows))
    # track-only markets (Anytime TD): how the model's would-be picks did
    tracked = [r for r in graded_picks(conn, sport, "close", include_track_only=True)
               if r["track_only"] and r["side"] in ("OVER", "UNDER")]
    out["track_only"] = _record(one_per_bet(tracked))
    # calibration: every scored line at close, model P(over) vs what happened
    rows = [r for r in graded_picks(conn, sport, "close") if not r["void"] and r["actual"] is not None]
    for lo in [i / 10 for i in range(10)]:
        b = [r for r in rows if lo <= r["p_over"] < lo + 0.1]
        if len(b) >= 20:
            hit = sum(r["actual"] > r["line"] for r in b) / len(b)
            out["calibration"].append({"predicted": sum(r["p_over"] for r in b) / len(b), "actual": hit,
                                       "market": sum(r["market_prob_over"] for r in b) / len(b), "n": len(b)})
    return out


def _record(rows):
    settled = [r for r in rows if r["outcome"] in ("win", "loss", "push")]
    risked = sum(1 for r in settled if r["outcome"] != "push")
    units = sum(r["profit"] for r in settled)
    clv = [r["clv"] for r in rows if r.get("clv") is not None]
    return {"picks": len(rows), "settled": len(settled), "wins": sum(r["outcome"] == "win" for r in settled),
            "losses": sum(r["outcome"] == "loss" for r in settled),
            "pushes": sum(r["outcome"] == "push" for r in settled),
            "voids": sum(r["outcome"] == "void" for r in rows), "units": units,
            "roi": units / risked if risked else None,
            "avg_clv": sum(clv) / len(clv) if clv else None, "clv_n": len(clv),
            "line_moved_for": sum(r.get("line_move") == 1 for r in rows),
            "line_moved_against": sum(r.get("line_move") == -1 for r in rows)}
