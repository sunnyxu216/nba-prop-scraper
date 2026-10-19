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
from spm.store import health, pg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    envfile.load()
    with pg.connect() as conn:
        s = health.status(conn)
        found = health.problems(s)
        report = health.weekly_report(conn) if args.report else ""
    last = s["last_seen"].strftime("%Y-%m-%d %H:%M UTC") if s["last_seen"] else "never"
    summary = [
        "## Pipeline health: " + ("FAILING" if found else "OK"), "",
        f"- Lines last seen: {last}",
        f"- Games in the next 36h: {s['upcoming_games']}",
        f"- Old games still ungraded: {s['ungraded_old_games']}",
        *[f"- **{p}**" for p in found],
    ]
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
