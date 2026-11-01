"""One-time cleanup to get Supabase back under the free tier (Nov 2026).

    python scripts/slim_db.py           # show what would be removed and the current sizes
    python scripts/slim_db.py --apply   # do it (takes a few minutes)

Steps, in order:
  1. delete pass rows logged before the closing checkpoint (new runs no longer write them),
     and queue every archived-but-not-yet-deleted game to be re-copied to MotherDuck, so the
     archive's row counts match Supabase again;
  2. run the archive now (copy + verify, then remove games older than 3 days from Supabase);
  3. VACUUM FULL the big tables: Postgres reuses deleted space but never shrinks the files, and
     the free tier's limit counts file size. VACUUM FULL writes a compacted copy before dropping
     the old file, so the database briefly grows by the table's live data: each table is only
     done if that peak stays under PEAK_LIMIT_MB, smallest copy first so each one frees room
     for the next. Each table is locked for seconds to a minute; a collector run that hits the
     lock waits and then continues;
  4. drop leftover test_* schemas from test runs that didn't clean up.
Needs DATABASE_URL and MOTHERDUCK_TOKEN in .env.
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spm import envfile  # noqa: E402
from spm.store import pg  # noqa: E402

BIG_TABLES = ("offers", "predictions", "predictions_latest", "offers_current", "grades")
PEAK_LIMIT_MB = 490      # free tier is 500 MB; the live estimate already overstates the peak
TEST_SCHEMA = re.compile(r"test_[0-9a-f]{12}")


def sizes(cur):
    cur.execute("select pg_database_size(current_database()) / 1e6")
    total = cur.fetchone()[0]
    cur.execute("select relname, pg_total_relation_size(relid) / 1e6 from pg_stat_user_tables "
                "where schemaname = 'public' order by 2 desc limit 6")
    return f"database {total:.0f} MB; " + ", ".join(f"{t} {mb:.0f} MB" for t, mb in cur.fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    envfile.load()
    with pg.connect() as conn, conn.cursor() as cur:
        print("Before:", sizes(cur))
        cur.execute("select count(*) from predictions where model_call = 'PASS' and checkpoint <> 'close'")
        passes = cur.fetchone()[0]
        cur.execute("select count(*) from archive_log where deleted_at is null")
        requeue = cur.fetchone()[0]
        cur.execute("select nspname from pg_namespace where nspname like 'test\\_%'")
        schemas = [s for (s,) in cur.fetchall() if TEST_SCHEMA.fullmatch(s)]
        print(f"1. {passes:,} early pass rows to delete; {requeue} archived games to re-copy")
        print("2. archive: copy + verify, remove games older than 3 days")
        print(f"3. VACUUM FULL {', '.join(BIG_TABLES)}")
        print(f"4. drop {len(schemas)} leftover test schemas")
        conn.commit()
        if not args.apply:
            print("\nNothing changed. Re-run with --apply to do it.")
            return 0

        with conn.transaction():
            cur.execute("delete from predictions where model_call = 'PASS' and checkpoint <> 'close'")
            cur.execute("delete from archive_log where deleted_at is null")
        print("1. done")

    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "archive.py")], cwd=ROOT)
    if r.returncode:
        print("2. archive failed: stopping before VACUUM (nothing else was changed)")
        return 1
    print("2. done")

    conn = pg.connect()
    conn.autocommit = True          # VACUUM can't run inside a transaction
    done = set()
    with conn, conn.cursor() as cur:
        for t in BIG_TABLES:                   # names from BIG_TABLES above, never from input
            cur.execute(f"vacuum (analyze) {t}")   # frees deleted rows for reuse and refreshes counts
        while True:
            cur.execute("select pg_database_size(current_database()) / 1e6")
            total = float(cur.fetchone()[0])
            # space each compacted copy needs: table size x share of the file that is row data (+20%
            # page overhead). Not n_dead_tup: the plain VACUUM above resets it to 0 while the file
            # stays full of freed space
            todo = []
            for t in BIG_TABLES:
                if t in done:
                    continue
                cur.execute(f"select coalesce(sum(pg_column_size(x.*)), 0) / 1e6, pg_relation_size(%s) / 1e6, "
                            f"pg_total_relation_size(%s) / 1e6 from {t} x", (t, t))
                data, heap, size = map(float, cur.fetchone())
                live = size * min(1.0, 1.2 * data / heap) if heap else 0.0
                if size - live > 5:                                       # skip if under 5 MB to gain
                    todo.append((live, t, size))
            todo.sort()
            if not todo:
                break
            live, t, size = todo[0]
            if total + live > PEAK_LIMIT_MB:
                print(f"3. skipped {t}: compacting needs ~{live:.0f} MB extra and the database is "
                      f"{total:.0f} MB (limit {PEAK_LIMIT_MB}); the deleted space is still reused")
                done.add(t)
                continue
            cur.execute(f"vacuum full {t}")
            print(f"3. compacted {t}: {size:.0f} MB -> ~{live:.0f} MB")
            done.add(t)
        for s in schemas:
            cur.execute(f'drop schema "{s}" cascade')   # names matched test_<12 hex> above
        print(f"4. dropped {len(schemas)} test schemas")
        print("After:", sizes(cur))
    return 0


if __name__ == "__main__":
    sys.exit(main())
