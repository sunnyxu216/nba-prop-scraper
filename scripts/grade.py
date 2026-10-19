"""Grade closing lines of finished games (incremental: only games not yet graded).

    python scripts/grade.py                         # grade whatever is pending
    python scripts/grade.py --regrade-since 2026-10-20   # redo grades after a stat correction
    python scripts/grade.py --check-source               # can we reach the box-score source?

Needs DATABASE_URL (env var or .env). Run daily by the "Grade lines" workflow.
Exit codes: 0 = ok (including nothing to do), 1 = error.
"""

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import boxscores, envfile  # noqa: E402
from spm.store import grading, pg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regrade-since", type=dt.date.fromisoformat)
    ap.add_argument("--check-source", action="store_true",
                    help="fetch a known past day of box scores and report, without touching the database")
    args = ap.parse_args()
    if args.check_source:
        day = dt.date(2026, 4, 12)   # last day of the 2025-26 regular season
        rows = boxscores.fetch(day, day)
        print(f"Box-score source reachable: {len(rows)} player rows for {day}")
        return 0 if rows else 1
    envfile.load()
    with pg.connect() as conn:
        pg.migrate(conn)
        if args.regrade_since:
            print(f"Deleted {grading.delete_grades_since(conn, args.regrade_since)} grades to regrade")
        events = grading.pending_events(conn)
        if not events:
            print("Nothing to grade.")
            return 0
        dates = sorted({grading.et_date(start) for _, start, _, _ in events})
        rows = boxscores.fetch(dates[0], dates[-1])
        print(f"{len(events)} finished games pending ({dates[0]} to {dates[-1]}); "
              f"loaded {grading.upsert_results(conn, rows)} box-score rows")
        graded, voided, skipped = grading.grade_events(conn, events)
        print(f"Graded {graded} closing lines, voided {voided} (player didn't play); "
              f"{len(skipped)} games still waiting for box scores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
