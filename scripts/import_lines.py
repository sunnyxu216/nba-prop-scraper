"""One-time import of line history collected before Postgres, replayed in time order.

    python scripts/import_lines.py            # import
    python scripts/import_lines.py --dry-run  # just show what would be imported

Sources of history:
* the `data` branch on GitHub (offers/*.csv from the cloud collector, Offer format);
* data/lines/lines.csv on this PC (the older local collector's format, which has
  no source event ids: those rows are matched to events by date and teams).

Every snapshot goes through the same pg.record() as live collection, oldest
first, so change detection and idempotency work exactly as they do live.
Re-running the import inserts nothing new.
"""

import csv
import datetime as dt
import io
import os
import subprocess
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spm import envfile  # noqa: E402
from spm.odds import Offer  # noqa: E402

LEGACY_CSV = os.path.join(ROOT, "data", "lines", "lines.csv")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def cloud_offers():
    """Offers from the data branch (offers/YYYY-MM-DD.csv)."""
    git("fetch", "-q", "origin", "data")
    files = [f for f in git("ls-tree", "--name-only", "origin/data", "offers/").split() if f.endswith(".csv")]
    out = []
    for f in files:
        for row in csv.DictReader(io.StringIO(git("show", f"origin/data:{f}"))):
            out.append(Offer(**{**row, "line": float(row["line"]), "over_odds": int(row["over_odds"]),
                                "under_odds": int(row["under_odds"])}))
    return out


def to_utc(local_naive):
    """Old snapshots are local wall-clock times without a zone."""
    return dt.datetime.fromisoformat(local_naive).astimezone().astimezone(dt.timezone.utc) \
        .isoformat().replace("+00:00", "Z")


def local_date(utc_iso):
    return dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone().date().isoformat()


def legacy_offers(events):
    """Offers from the old local lines.csv. events: {(local date, home, away): (event_id, start_time)}."""
    if not os.path.exists(LEGACY_CSV):
        return [], 0
    out, unmatched = [], 0
    with open(LEGACY_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            home, away = (r["team"], r["opponent"]) if r["home"] == "1" else (r["opponent"], r["team"])
            match = events.get((r["game_date"], home, away))
            if match:
                event_id, start = match
            else:
                unmatched += 1
                event_id, start = f"legacy:{r['game_date']}:{away}@{home}", r["tipoff"]
            out.append(Offer(source=r["source"], sport="nba", event_id=event_id, start_time=start,
                             home_team=home, away_team=away, player_name=r["player"], source_player_id="",
                             team=r["team"], market=r["stat"], book=r["book"], line=float(r["line"]),
                             over_odds=int(r["over_odds"]), under_odds=int(r["under_odds"]),
                             observed_at=to_utc(r["snapshot"])))
    return out, unmatched


def main():
    dry = "--dry-run" in sys.argv
    envfile.load()
    cloud = cloud_offers()
    events = {(local_date(o.start_time), o.home_team, o.away_team): (o.event_id, o.start_time)
              for o in cloud if o.start_time}
    legacy, unmatched = legacy_offers(events)
    snapshots = defaultdict(list)
    for o in cloud + legacy:
        snapshots[o.observed_at].append(o)
    print(f"cloud offers: {len(cloud)}, local offers: {len(legacy)} ({unmatched} matched to no cloud event), "
          f"snapshots: {len(snapshots)}")
    if dry:
        return
    from spm.store import pg
    with pg.connect() as conn:
        pg.migrate(conn)
        total = 0
        for observed in sorted(snapshots):
            total += pg.record(conn, snapshots[observed])
    print(f"appended {total} new-or-moved offers")


if __name__ == "__main__":
    main()
