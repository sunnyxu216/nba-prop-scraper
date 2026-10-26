"""Nightly archive: copy finished games to MotherDuck, then trim Supabase to the last 7 days.

    python scripts/archive.py              # copy + verify, delete verified games older than 7 days,
                                           # refresh the paper-trading summary
    python scripts/archive.py --no-delete  # copy + verify only (nothing removed from Supabase)

Run daily at 10:30 UTC by the "Archive" workflow (Supabase pg_cron), after grading.
Needs DATABASE_URL and MOTHERDUCK_TOKEN. See spm/store/archive.py for how it stays safe.
Exit codes: 0 = ok, 1 = error (including a failed verification: then nothing is deleted).
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile  # noqa: E402
from spm.store import archive, pg  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-delete", action="store_true", help="copy and verify only")
    args = ap.parse_args()
    envfile.load()
    t = time.time()
    with pg.connect() as conn:
        pg.migrate(conn)
        con = archive.connect()
        try:
            ids = archive.pending(conn)
            copied = archive.copy(con, conn, ids)
            rows = {tb: sum(c[tb] for c in copied.values()) for tb in archive.BY_EVENT}
            print(f"Archived {len(copied)} games to MotherDuck (verified): "
                  + ", ".join(f"{n:,} {tb}" for tb, n in rows.items()))
            if args.no_delete:
                print("--no-delete: nothing removed from Supabase.")
            else:
                gone = archive.delete(conn, archive.deletable(conn))
                print("Removed from Supabase (older than 7 days, archived and verified): "
                      + (", ".join(f"{n:,} {tb}" for tb, n in gone.items() if n) or "nothing yet"))
            sums = archive.store_summaries(con, conn)
            print(f"Paper-trading summary refreshed for: {', '.join(sums) or 'no sports yet'}")
        finally:
            con.close()
    print(f"Done in {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
