"""Long-term archive in MotherDuck (DuckDB in the cloud); Supabase keeps only recent days.

Nightly (scripts/archive.py, after grading):

  1. copy every finished game's rows (events, offers, grades, predictions, and that day's box
     scores) from Supabase to MotherDuck. Per game, the archive copy is replaced (delete +
     insert), so re-running is harmless and regrades/stat corrections carry over;
  2. verify: per game, the archive's row counts must equal Supabase's;
  3. after HOT_DAYS, delete the verified, fully graded games' rows from Supabase. Never deleted:
     `events` (tiny; everything references them), box scores (`results`, `results_nfl`: the NBA
     scorer needs this season's) and `artifacts` (the NBA model bundle, not archived at all);
  4. recompute paper-trading results over the whole archive and store them in Supabase
     (`paper_summary`), so the dashboard and weekly report see full history without DuckDB.

The copy runs inside DuckDB with Supabase attached READ_ONLY, so it cannot modify Supabase;
deletes go through the normal Postgres connection, only after verification. The Postgres
password reaches DuckDB through PG* environment variables, never through SQL text, so it can't
appear in an error message or a log.

Every archived row keeps its `sport` and `source` columns, so scraped data can still be excluded.
"""

import contextlib
import datetime as dt
import json
import os
import urllib.parse

HOT_DAYS = 7                                  # days of data kept in Supabase
SETTLED_AFTER = dt.timedelta(hours=8)         # a game this old has had its grading run
BY_EVENT = ("offers", "grades", "predictions")   # tables archived per game (event_id)
# Box scores, archived per game date but never deleted from Supabase: the NBA scorer rebuilds this
# season's game logs from `results`, and both tables are small (a few MB a season).
RESULTS = ("results", "results_nfl")
MD_DATABASE = "props"


# --------------------------------------------------------------------------- connections

def _pg_env(url):
    """Expose a Postgres URL to DuckDB's postgres extension as libpq environment variables."""
    u = urllib.parse.urlparse(url)
    os.environ.update({"PGHOST": u.hostname or "", "PGPORT": str(u.port or 5432),
                       "PGUSER": urllib.parse.unquote(u.username or ""),
                       "PGPASSWORD": urllib.parse.unquote(u.password or ""),
                       "PGDATABASE": (u.path or "/postgres").lstrip("/") or "postgres"})


def connect(pg_url=None, target=None):
    """A DuckDB connection with the archive as the default database and Supabase attached as `pg`.

    target: "md:" (MotherDuck, needs MOTHERDUCK_TOKEN) or a local .duckdb file path (tests).
    """
    import duckdb
    target = target or "md:"
    # before DuckDB loads its postgres extension: on Windows the extension's libpq reads the
    # environment once, when it's loaded, so variables set afterwards aren't seen
    _pg_env(pg_url or os.environ["DATABASE_URL"])
    if target.startswith("md:"):
        con = duckdb.connect("md:", config={"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]})
        con.execute(f"create database if not exists {MD_DATABASE}")
        con.execute(f"use {MD_DATABASE}")
    else:
        con = duckdb.connect(target)
    con.execute("install postgres; load postgres")
    con.execute("attach '' as pg (type postgres, read_only)")
    return con


def _columns(con, catalog, schema, table):
    return [r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_catalog = ? and table_schema = ? "
        "and table_name = ? order by ordinal_position", [catalog, schema, table]).fetchall()]


def _ensure_table(con, schema, table):
    """Create the archive table from Supabase's definition; add any columns added there since."""
    src = f"pg.{schema}.{table}"
    db = con.execute("select current_database()").fetchone()[0]
    have = _columns(con, db, "main", table)
    if not have:
        con.execute(f"create table main.{table} as select * from {src} limit 0")
        return
    for col in _columns(con, "pg", schema, table):
        if col not in have:
            typ = con.execute(f"select typeof({col}) from {src} limit 1").fetchone()
            con.execute(f"alter table main.{table} add column {col} {typ[0] if typ else 'varchar'}")


# --------------------------------------------------------------------------- copy

def pending(conn, now=None):
    """Games to (re)copy: finished, still holding rows in Supabase, and new or regraded since last copy."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with conn.transaction(), conn.cursor() as cur:     # committed reads: no transaction left open
        cur.execute("""
            select e.id from events e left join archive_log a on a.event_id = e.id
            where e.start_time < %s and (a.deleted_at is null)
              and exists (select 1 from offers o where o.event_id = e.id)
              and (a.event_id is null
                   or exists (select 1 from grades g where g.event_id = e.id and g.graded_at > a.archived_at)
                   or exists (select 1 from predictions p where p.event_id = e.id and p.scored_at > a.archived_at))
            order by e.id""", (now - SETTLED_AFTER,))
        return [r[0] for r in cur.fetchall()]


def _counts(con, catalog_schema, table, ids):
    rows = con.execute(f"select event_id, count(*) from {catalog_schema}.{table} where event_id in "
                       f"(select unnest(?::bigint[])) group by 1", [ids]).fetchall()
    return {e: n for e, n in rows}


def copy(con, conn, event_ids, schema="public", now=None):
    """Copy these games to the archive (replacing earlier copies) and verify per-game counts.

    Returns {event_id: {table: rows}}. Raises if any game's counts disagree (nothing is logged then).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not event_ids:
        return {}
    for t in ("events", *BY_EVENT, *RESULTS):
        _ensure_table(con, schema, t)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""select distinct (start_time at time zone 'UTC' - interval '5 hours')::date
                       from events where id = any(%s)""", (event_ids,))
        dates = [r[0] for r in cur.fetchall()]
    con.execute("begin")
    try:
        con.execute("delete from main.events where id in (select unnest(?::bigint[]))", [event_ids])
        con.execute(f"insert into main.events select * from pg.{schema}.events where id in (select unnest(?::bigint[]))",
                    [event_ids])
        for t in BY_EVENT:
            cols = ", ".join(_columns(con, "pg", schema, t))
            con.execute(f"delete from main.{t} where event_id in (select unnest(?::bigint[]))", [event_ids])
            con.execute(f"insert into main.{t} ({cols}) select {cols} from pg.{schema}.{t} "
                        f"where event_id in (select unnest(?::bigint[]))", [event_ids])
        for t in RESULTS:
            cols = ", ".join(_columns(con, "pg", schema, t))
            con.execute(f"delete from main.{t} where game_date in (select unnest(?::date[]))", [dates])
            con.execute(f"insert into main.{t} ({cols}) select {cols} from pg.{schema}.{t} "
                        f"where game_date in (select unnest(?::date[]))", [dates])
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    copied = {}
    for t in BY_EVENT:
        src, dst = _counts(con, f"pg.{schema}", t, event_ids), _counts(con, "main", t, event_ids)
        if src != dst:
            bad = sorted(e for e in set(src) | set(dst) if src.get(e) != dst.get(e))
            raise RuntimeError(f"archive verification failed for {t}: games {bad[:10]}")
        for e in event_ids:
            copied.setdefault(e, {})[t] = src.get(e, 0)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("""
            insert into archive_log (event_id, archived_at, offers, grades, predictions) values (%s, %s, %s, %s, %s)
            on conflict (event_id) do update set archived_at = excluded.archived_at, offers = excluded.offers,
                grades = excluded.grades, predictions = excluded.predictions""",
            [(e, now, c["offers"], c["grades"], c["predictions"]) for e, c in copied.items()])
    return copied


# --------------------------------------------------------------------------- delete

def deletable(conn, now=None):
    """Archived games older than HOT_DAYS whose Supabase rows still match the archived counts and
    that are fully graded (no closing line left ungraded)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""
            select a.event_id from archive_log a join events e on e.id = a.event_id
            where a.deleted_at is null and e.start_time < %s
              and a.offers = (select count(*) from offers o where o.event_id = a.event_id)
              and a.grades = (select count(*) from grades g where g.event_id = a.event_id)
              and a.predictions = (select count(*) from predictions p where p.event_id = a.event_id)
              and not exists (
                select 1 from offers o
                left join grades g on g.event_id = o.event_id and g.player_key = o.player_key
                                  and g.market = o.market and g.book = o.book
                where o.event_id = a.event_id and o.observed_at < e.start_time and g.event_id is null)
            order by 1""", (now - dt.timedelta(days=HOT_DAYS),))
        return [r[0] for r in cur.fetchall()]


def delete(conn, event_ids, results_before=None, now=None):
    """Remove archived games' rows from Supabase (events stay). Returns rows deleted per table."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {}
    if not event_ids:
        return out
    with conn.transaction(), conn.cursor() as cur:
        for t in ("predictions_latest", "predictions", "grades", "offers_current", "offers"):
            cur.execute(f"delete from {t} where event_id = any(%s)", (event_ids,))
            out[t] = cur.rowcount
        cur.execute("update archive_log set deleted_at = %s where event_id = any(%s)", (now, event_ids))
    return out


# --------------------------------------------------------------------------- paper summary

class _DuckCursor:
    """Just enough of a psycopg cursor for spm.store.paper's queries to run on DuckDB."""

    def __init__(self, con):
        self.con, self.result = con, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.result = self.con.execute(sql.replace("%s", "?"), list(params))
        return self

    def fetchall(self):
        return self.result.fetchall()

    def fetchone(self):
        return self.result.fetchone()

    @property
    def description(self):
        return self.result.description


class DuckConn:
    """The archive, presented to paper.summary() like a Postgres connection (read-only use)."""

    def __init__(self, con):
        self.con = con

    def cursor(self):
        return _DuckCursor(self.con)

    def transaction(self):
        return contextlib.nullcontext()


def store_summaries(con, conn, now=None):
    """Paper-trading results over the full archive, per sport, saved to Supabase's paper_summary."""
    from . import paper
    now = now or dt.datetime.now(dt.timezone.utc)
    sports = [r[0] for r in con.execute("select distinct sport from main.predictions").fetchall()] \
        if _columns(con, con.execute("select current_database()").fetchone()[0], "main", "predictions") else []
    done = {}
    for sport in sports:
        s = paper.summary(DuckConn(con), sport)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("""insert into paper_summary (sport, computed_at, summary) values (%s, %s, %s)
                           on conflict (sport) do update set computed_at = excluded.computed_at,
                               summary = excluded.summary""", (sport, now, json.dumps(s, default=str)))
        done[sport] = s
    return done


def paper_summary(conn, sport):
    """Paper-trading results for the dashboard / weekly report: the nightly full-history summary,
    or (before the first archive run) a live summary of what Supabase still holds."""
    from . import paper
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("select summary, computed_at from paper_summary where sport = %s", (sport,))
            row = cur.fetchone()
    except Exception:            # migration 007 not applied yet
        row = None
    if row:
        s = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {**s, "computed_at": str(row[1]), "scope": "archive"}
    return {**paper.summary(conn, sport), "scope": "recent"}


# --------------------------------------------------------------------------- health

def problems(conn, now=None):
    """Archive falling behind: finished games more than 2 days old that were never archived."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("""select count(*) from events e left join archive_log a on a.event_id = e.id
                           where e.start_time < %s and a.event_id is null
                             and exists (select 1 from offers o where o.event_id = e.id)""",
                        (now - dt.timedelta(days=2),))
            behind = cur.fetchone()[0]
    except Exception:            # migration 007 not applied yet
        return []
    return [f"Archive is behind: {behind} finished games older than 2 days were never copied to MotherDuck."] \
        if behind else []
