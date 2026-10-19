"""Take one snapshot of every available prop line, then grade finished games.

    python collect_lines.py

Run hourly by the "NBA Prop Model - collect lines" scheduled task (see README).
Writes a log to data/lines/collector.log. The app also snapshots every 3 hours
while it's open.
"""

import datetime as dt
import os
import traceback

from spm import collector, data

LOG = os.path.join(collector.LINES_DIR, "collector.log")


def log(msg):
    os.makedirs(collector.LINES_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    try:
        ds = data.load_dataset(log=lambda *a: None)  # also refreshes box scores used for grading
        collector.snapshot(ds, log=log)
        g = collector.grade(ds)
        log(f"Totals: {g['collected']} lines collected, {g['closing_lines']} closing lines, {g['graded']} graded")
    except Exception:
        log("FAILED\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
