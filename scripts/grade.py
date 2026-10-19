"""Grade closing lines of finished games (incremental: only games not yet graded).

    python scripts/grade.py                         # grade whatever is pending, every sport
    python scripts/grade.py --sport nfl             # one sport only
    python scripts/grade.py --regrade-since 2026-10-20   # redo grades after a stat correction
    python scripts/grade.py --check-source               # can we reach the box-score sources?

Needs DATABASE_URL (env var or .env). Run daily by the "Grade lines" workflow.
Exit codes: 0 = ok (including nothing to do), 1 = error in any sport (the others still run).
"""

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import boxscores, envfile  # noqa: E402
from spm.nfl import boxscores as nfl_boxscores  # noqa: E402
from spm.store import grading, pg  # noqa: E402

FETCH = {"nba": boxscores.fetch, "nfl": nfl_boxscores.fetch}
CHECK_DAY = {"nba": dt.date(2026, 4, 12),     # last day of the 2025-26 NBA regular season
             "nfl": dt.date(2026, 1, 4)}      # last Sunday of the 2025 NFL regular season


def grade_sport(conn, sport):
    events = grading.pending_events(conn, sport=sport)
    if not events:
        print(f"{sport.upper()}: nothing to grade.")
        return
    dates = sorted({grading.et_date(start) for _, start, _, _ in events})
    rows = FETCH[sport](dates[0], dates[-1])
    print(f"{sport.upper()}: {len(events)} finished games pending ({dates[0]} to {dates[-1]}); "
          f"loaded {grading.upsert_results(conn, rows, sport)} box-score rows")
    graded, voided, skipped = grading.grade_events(conn, events, sport)
    print(f"{sport.upper()}: graded {graded} closing lines, voided {voided} (player didn't play); "
          f"{len(skipped)} games still waiting for box scores")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=sorted(FETCH), help="default: every sport")
    ap.add_argument("--regrade-since", type=dt.date.fromisoformat)
    ap.add_argument("--check-source", action="store_true",
                    help="fetch a known past day of box scores and report, without touching the database")
    args = ap.parse_args()
    sports = [args.sport] if args.sport else list(grading.GRADED_SPORTS)
    if args.check_source:
        ok = True
        for sport in sports:
            day = CHECK_DAY[sport]
            rows = FETCH[sport](day, day)
            print(f"{sport.upper()} box-score source reachable: {len(rows)} player rows for {day}")
            ok &= bool(rows)
        return 0 if ok else 1
    envfile.load()
    failed = []
    with pg.connect() as conn:
        pg.migrate(conn)
        if args.regrade_since:
            print(f"Deleted {grading.delete_grades_since(conn, args.regrade_since)} grades to regrade")
        for sport in sports:
            try:
                grade_sport(conn, sport)
            except Exception as e:   # one sport's source being down mustn't block the other's grading
                conn.rollback()
                failed.append(sport)
                print(f"::error::{sport.upper()} grading failed: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
