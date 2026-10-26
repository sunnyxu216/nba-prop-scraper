"""Schedule the GitHub workflows from Supabase (pg_cron + pg_net), replacing GitHub's own
cron, which proved unreliable for this repo (scheduled runs never fired).

    python scripts/setup_scheduler.py            # install / update (safe to re-run)
    python scripts/setup_scheduler.py --status   # jobs, recent runs, recent GitHub responses
    python scripts/setup_scheduler.py --test     # dispatch Collect lines once, show GitHub's answer

How it works: pg_cron runs a SQL statement on a timetable inside the database;
that statement calls public.dispatch_workflow(), which uses pg_net to POST to
GitHub's "run workflow" endpoint (the same as clicking Run workflow). The token
lives encrypted in Supabase Vault under the name github_dispatch_token.

Needs in .env: DATABASE_URL, and GITHUB_DISPATCH_TOKEN (a fine-grained token for
this one repo with "Actions: Read and write" only). Not part of migrations/
because CI's throwaway Postgres has no pg_cron/pg_net/Vault.
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spm import envfile  # noqa: E402
from spm.store import pg  # noqa: E402

REPO = "sunnyxu216/nba-prop-scraper"
SECRET = "github_dispatch_token"
JOBS = [  # (pg_cron job name, cron in UTC, workflow file)
    ("collect-lines", "17 * * * *", "collect.yml"),
    ("grade-lines", "0 10 * * *", "grade.yml"),
    ("health", "0 15 * * *", "health.yml"),
    ("score-nba", "42 * * * *", "score-nba.yml"),
    ("score-nfl", "37 * * * *", "score-nfl.yml"),   # 20 min after collection; no-op without NFL lines
]


def install(cur, token, repo):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError(f"bad repo name {repo!r}")
    cur.execute("create extension if not exists pg_cron")
    cur.execute("create extension if not exists pg_net")
    cur.execute("select id from vault.secrets where name = %s", (SECRET,))
    row = cur.fetchone()
    if row:
        cur.execute("select vault.update_secret(%s, %s)", (row[0], token))
    else:
        cur.execute("select vault.create_secret(%s, %s, %s)",
                    (token, SECRET, f"GitHub token that can start workflows on {repo}"))
    cur.execute(f"""
        create or replace function public.dispatch_workflow(workflow text) returns bigint
        language sql security definer set search_path = public as $$
            select net.http_post(
                url := 'https://api.github.com/repos/{repo}/actions/workflows/' || workflow || '/dispatches',
                headers := jsonb_build_object(
                    'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets
                                                   where name = '{SECRET}'),
                    'Accept', 'application/vnd.github+json',
                    'User-Agent', 'supabase-pg-cron',
                    'Content-Type', 'application/json'),
                body := jsonb_build_object('ref', 'main'),
                timeout_milliseconds := 10000)
        $$""")
    # only the database owner may call it (the Data API roles must never reach the token)
    cur.execute("revoke all on function public.dispatch_workflow(text) from public")
    cur.execute("""do $$ begin
        if exists (select 1 from pg_roles where rolname = 'anon') then
            revoke all on function public.dispatch_workflow(text) from anon, authenticated;
        end if; end $$""")
    for name, schedule, workflow in JOBS:
        cur.execute("select cron.schedule(%s, %s, %s)",
                    (name, schedule, f"select public.dispatch_workflow('{workflow}')"))


def status(cur):
    cur.execute("select jobname, schedule, active from cron.job order by jobname")
    print("Scheduled jobs:")
    for r in cur.fetchall():
        print(f"  {r[0]:14s} {r[1]:12s} active={r[2]}")
    cur.execute("""select j.jobname, d.status, d.start_time, left(d.return_message, 80)
                   from cron.job_run_details d join cron.job j using (jobid)
                   order by d.start_time desc limit 8""")
    print("Recent pg_cron runs:")
    for r in cur.fetchall():
        print(f"  {r[2]:%Y-%m-%d %H:%M} UTC  {r[0]:14s} {r[1]}  {r[3] or ''}")
    cur.execute("""select created, status_code, left(coalesce(content, error_msg, ''), 80)
                   from net._http_response order by created desc limit 8""")
    print("Recent GitHub responses (204 = workflow started):")
    for r in cur.fetchall():
        print(f"  {r[0]:%Y-%m-%d %H:%M} UTC  HTTP {r[1]}  {r[2]}")


def test(cur, conn):
    cur.execute("select public.dispatch_workflow('collect.yml')")
    req = cur.fetchone()[0]
    conn.commit()
    for _ in range(20):
        time.sleep(1)
        cur.execute("select status_code, left(coalesce(content, error_msg, ''), 200) "
                    "from net._http_response where id = %s", (req,))
        row = cur.fetchone()
        if row:
            print(f"GitHub answered HTTP {row[0]} {row[1]}")
            return row[0] == 204
    print("No answer yet (pg_net is asynchronous); check --status in a minute")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    envfile.load()
    with pg.connect() as conn, conn.cursor() as cur:
        if args.status:
            return status(cur)
        if args.test:
            return 0 if test(cur, conn) else 1
        token = os.environ.get("GITHUB_DISPATCH_TOKEN")
        if not token:
            sys.exit("Add GITHUB_DISPATCH_TOKEN=... to .env first (see the docstring).")
        install(cur, token, os.environ.get("GITHUB_REPO", REPO))
        conn.commit()
        print("Installed:", ", ".join(f"{n} ({s})" for n, s, _ in JOBS))


if __name__ == "__main__":
    sys.exit(main())
