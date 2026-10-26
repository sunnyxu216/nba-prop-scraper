"""Score open NBA lines with the trained model and log them for paper trading.

    python scripts/score_nba.py                  # hourly job: score open lines, write predictions
    python scripts/score_nba.py --bundle-version # print the model bundle version (workflow cache key)
    python scripts/score_nba.py --backfill       # re-score graded games as of each game day; prints
                                                 # a scorecard, writes nothing

Run hourly by the "Score NBA" workflow (Supabase pg_cron). Exits early, before loading the
model, when no NBA lines are open. Needs DATABASE_URL and the model bundle uploaded by
scripts/publish_nba_model.py (cached between runs by the workflow, keyed by its version).

The backfill is approximate: injury reports from those days weren't saved, so it scores as if
nobody was listed (players who sat out are voided anyway; teammates' absences aren't seen).
Exit codes: 0 = ok (including nothing to score), 1 = error.
"""

import argparse
import datetime as dt
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile  # noqa: E402
from spm.store import paper, pg  # noqa: E402


def score_open(conn):
    t = time.time()
    offers = paper.open_offers(conn, "nba")
    if not offers:
        print("No open NBA lines: nothing to score.")
        return 0
    from spm import nba_cloud      # heavy imports (numpy, lightgbm) only when there's work
    version = nba_cloud.prepare(conn)
    results = nba_cloud.Scorer(version).score(offers)
    logged, latest = paper.record(conn, offers, results, "nba")
    scored = [r for r in results if not r.get("skipped")]
    bets = [r for r in scored if r["decision"] != "PASS"]
    print(f"Scored {len(scored)} of {len(offers)} open NBA lines ({len(offers) - len(scored)} skipped: "
          f"rookies, unknown players or markets); {len(bets)} recommended bets; "
          f"{logged} checkpoint rows and {latest} board rows written in {time.time() - t:.0f}s")
    return 0


BACKFILL_SQL = """
    select g.event_id, g.source, g.player_key, g.player_name, '' as team, g.market, g.book, g.line::float,
           g.over_odds, g.under_odds, g.closing_observed_at as last_seen, e.home_team, e.away_team,
           e.start_time, g.actual::float as actual
    from grades g join events e on e.id = g.event_id
    where g.sport = 'nba' and g.result <> 'void' and (e.start_time - interval '5 hours')::date = %s"""


def backfill(conn):
    from spm import ev, nba_cloud
    with conn.cursor() as cur:
        cur.execute("""select distinct (e.start_time - interval '5 hours')::date from grades g
                       join events e on e.id = g.event_id where g.sport = 'nba' order by 1""")
        days = [r[0] for r in cur.fetchall()]
    picks = []
    for day in days:
        with conn.cursor() as cur:
            cur.execute(BACKFILL_SQL, (day,))
            cols = [d.name for d in cur.description]
            offers = [dict(zip(cols, r)) for r in cur.fetchall()]
        version = nba_cloud.prepare(conn, before=day.isoformat(), log=lambda *a: None)
        results = nba_cloud.Scorer(version, date=day.isoformat(), injuries={}, log=lambda *a: None).score(offers)
        best = {}   # one pick per player, market and game: the book with the best EV (what you'd bet)
        for o, r in zip(offers, results):
            if r.get("skipped") or r["decision"] == "PASS":
                continue
            side_ev = r["ev_over"] if r["decision"] == "OVER" else r["ev_under"]
            key = (o["event_id"], o["player_key"], o["market"])
            if key not in best or side_ev > best[key][2]:
                best[key] = (o, r, side_ev)
        for o, r, side_ev in best.values():
            odds = o["over_odds"] if r["decision"] == "OVER" else o["under_odds"]
            if o["actual"] == o["line"]:
                outcome, units = "push", 0.0
            elif (o["actual"] > o["line"]) == (r["decision"] == "OVER"):
                outcome, units = "win", ev.profit_per_unit(odds)
            else:
                outcome, units = "loss", -1.0
            picks.append({"day": day, "market": o["market"], "side": r["decision"], "outcome": outcome,
                          "units": units, "ev": side_ev, "player": o["player_name"], "line": o["line"],
                          "actual": o["actual"], "odds": odds})
        n = [p for p in picks if p["day"] == day]
        print(f"{day}: {len(offers)} graded lines, {len(n)} picks, "
              f"{sum(p['outcome'] == 'win' for p in n)}-{sum(p['outcome'] == 'loss' for p in n)}, "
              f"{sum(p['units'] for p in n):+.2f}u")

    def line(label, rows):
        risked = sum(p["outcome"] != "push" for p in rows)
        units = sum(p["units"] for p in rows)
        roi = f"{units / risked:+.1%}" if risked else "n/a"
        print(f"  {label:12s} {len(rows):4d} picks  {sum(p['outcome'] == 'win' for p in rows):3d}-"
              f"{sum(p['outcome'] == 'loss' for p in rows):<3d}  {units:+7.2f}u  ROI {roi}")
    print("\nBackfill scorecard (closing lines, 1 unit per pick, best book per pick):")
    line("All", picks)
    for side in ("OVER", "UNDER"):
        line(side.title() + "s", [p for p in picks if p["side"] == side])
    by_market = defaultdict(list)
    for p in picks:
        by_market[p["market"]].append(p)
    for m, rows in sorted(by_market.items(), key=lambda t: -len(t[1])):
        line(m, rows)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle-version", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()
    envfile.load()
    with pg.connect() as conn:
        if args.bundle_version:
            from spm.store import artifacts
            print(artifacts.version_of(conn, "nba_model") or "none")
            return 0
        pg.migrate(conn)
        return backfill(conn) if args.backfill else score_open(conn)


if __name__ == "__main__":
    sys.exit(main())
