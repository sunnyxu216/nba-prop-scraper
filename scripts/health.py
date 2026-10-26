"""Pipeline health check (and optional weekly report).

    python scripts/health.py            # exit 1 if collection is stale or grading is behind
    python scripts/health.py --report   # also print the weekly markdown report

Writes the result to $GITHUB_STEP_SUMMARY when run in GitHub Actions, and the
report to report.md when --report is given (the workflow posts it as an issue
comment). A failing check makes GitHub email the repo owner.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile  # noqa: E402
from spm.store import archive, grading, health, pg  # noqa: E402

SPORTS = ("nba", "nfl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    envfile.load()
    with pg.connect() as conn:
        statuses = [health.status(conn, sport=sp, check_grading=sp in grading.GRADED_SPORTS) for sp in SPORTS]
        report = health.weekly_report(conn) if args.report else ""
    found = [f"{s['sport'].upper()}: {p}" for s in statuses for p in health.problems(s)]
    with pg.connect() as conn:
        found += archive.problems(conn) + health.size_problems(conn)
    summary = ["## Pipeline health: " + ("FAILING" if found else "OK"), ""]
    for s in statuses:
        last = s["last_seen"].strftime("%Y-%m-%d %H:%M UTC") if s["last_seen"] else "never"
        graded = s["ungraded_old_games"] if s["sport"] in grading.GRADED_SPORTS else "not graded yet"
        summary.append(f"- {s['sport'].upper()}: lines last seen {last}; games in the next 36h: "
                       f"{s['upcoming_games']}; old games still ungraded: {graded}")
    summary += [f"- **{p}**" for p in found]
    text = "\n".join(summary) + ("\n\n" + report if report else "")
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if report:
        with open("report.md", "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
