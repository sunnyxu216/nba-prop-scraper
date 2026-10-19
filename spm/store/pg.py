"""Postgres storage for collected offers (Supabase in production).

Change detection happens inside the database, in one transaction per snapshot:

  1. load the snapshot into a temporary staging table (one round trip, COPY);
  2. upsert its events and attach event ids;
  3. append to `offers` every staged row that is new or whose line/price differs
     from `offers_current`;
  4. upsert `offers_current` (moves update changed_at; everything updates last_seen).

Because of the unique constraints, re-running the same snapshot inserts nothing.
"""

import os
import re
from pathlib import Path

import psycopg

from ..data import norm_name

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def connect(url=None, schema=None):
    """Connect using DATABASE_URL (or `url`). `schema` scopes everything to one schema (tests)."""
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements
    conn = psycopg.connect(url, prepare_threshold=None, connect_timeout=15)
    if schema:
        with conn.cursor() as cur:
            cur.execute(f'create schema if not exists "{schema}"')
            cur.execute(f'set search_path to "{schema}"')
        conn.commit()
    return conn


# --------------------------------------------------------------------------- migrations

def migrate(conn):
    """Apply migrations/NNN_*.sql that haven't been applied yet, each in its own transaction."""
    with conn.cursor() as cur:
        cur.execute("create table if not exists schema_migrations "
                    "(version text primary key, applied_at timestamptz not null default now())")
        cur.execute("select version from schema_migrations")
        done = {r[0] for r in cur.fetchall()}
    conn.commit()
    applied = []
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        version = path.stem
        if version in done:
            continue
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("insert into schema_migrations (version) values (%s)", (version,))
        applied.append(version)
    return applied


def sync_sources(conn, registry):
    """Mirror the odds-source registry (name, licensed) into the sources table."""
    with conn.transaction(), conn.cursor() as cur:
        for name, cls in registry.items():
            cur.execute("insert into sources (name, licensed, description) values (%s, %s, %s) "
                        "on conflict (name) do update set licensed = excluded.licensed, "
                        "description = excluded.description",
                        (name, cls.licensed, (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""))


# --------------------------------------------------------------------------- writes

STAGE_COLUMNS = ["sport", "source", "source_event_id", "start_time", "home_team", "away_team", "player_key",
                 "player_name", "source_player_id", "team", "market", "book", "line", "over_odds", "under_odds",
                 "observed_at"]


def player_key(name):
    return norm_name(name)


def _stage_rows(offers):
    """Offers -> staging tuples, dropping duplicates within the snapshot (last one wins)."""
    rows = {}
    for o in offers:
        key = (o.source, o.event_id, player_key(o.player_name), o.market, o.book)
        rows[key] = (o.sport, o.source, o.event_id, o.start_time or None, o.home_team, o.away_team,
                     key[2], o.player_name, o.source_player_id, o.team, o.market, o.book, o.line,
                     o.over_odds, o.under_odds, o.observed_at)
    return list(rows.values())


def record(conn, offers):
    """Store one snapshot. Returns the number of new-or-moved offers appended."""
    rows = _stage_rows(offers)
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""
            create temporary table stage (
                sport text, source text, source_event_id text, start_time timestamptz,
                home_team text, away_team text, player_key text, player_name text,
                source_player_id text, team text, market text, book text, line numeric(6, 1),
                over_odds integer, under_odds integer, observed_at timestamptz, event_id bigint
            ) on commit drop""")
        with cur.copy(f"copy stage ({', '.join(STAGE_COLUMNS)}) from stdin") as copy:
            for r in rows:
                copy.write_row(r)

        cur.execute("""
            insert into events (sport, source, source_event_id, start_time, home_team, away_team)
            select distinct on (source, source_event_id) sport, source, source_event_id, start_time,
                   home_team, away_team
            from stage
            order by source, source_event_id, observed_at desc
            on conflict (source, source_event_id) do update
                set start_time = coalesce(excluded.start_time, events.start_time),
                    home_team = excluded.home_team, away_team = excluded.away_team""")
        cur.execute("""
            update stage s set event_id = e.id
            from events e where e.source = s.source and e.source_event_id = s.source_event_id""")

        cur.execute("""
            insert into offers (event_id, sport, source, player_key, player_name, source_player_id, team,
                                market, book, line, over_odds, under_odds, observed_at)
            select s.event_id, s.sport, s.source, s.player_key, s.player_name, s.source_player_id, s.team,
                   s.market, s.book, s.line, s.over_odds, s.under_odds, s.observed_at
            from stage s
            left join offers_current c
              on c.event_id = s.event_id and c.player_key = s.player_key
             and c.market = s.market and c.book = s.book
            where c.event_id is null
               or (c.line, c.over_odds, c.under_odds) is distinct from (s.line, s.over_odds, s.under_odds)
            on conflict do nothing""")
        appended = cur.rowcount

        cur.execute("""
            insert into offers_current (event_id, sport, source, player_key, player_name, source_player_id,
                                        team, market, book, line, over_odds, under_odds,
                                        first_seen, changed_at, last_seen)
            select event_id, sport, source, player_key, player_name, source_player_id, team, market, book,
                   line, over_odds, under_odds, observed_at, observed_at, observed_at
            from stage
            on conflict (event_id, player_key, market, book) do update set
                changed_at = case
                    when (offers_current.line, offers_current.over_odds, offers_current.under_odds)
                         is distinct from (excluded.line, excluded.over_odds, excluded.under_odds)
                    then excluded.last_seen else offers_current.changed_at end,
                line = excluded.line, over_odds = excluded.over_odds, under_odds = excluded.under_odds,
                player_name = excluded.player_name, source_player_id = excluded.source_player_id,
                team = excluded.team,
                last_seen = greatest(offers_current.last_seen, excluded.last_seen)""")
    return appended


def current_lines(conn, max_age_hours=6):
    """Open lines for upcoming games, as last seen by the collector (newest prices)."""
    with conn.cursor() as cur:
        cur.execute("""
            select oc.source, oc.player_name, oc.team, oc.market, oc.book, oc.line::float, oc.over_odds,
                   oc.under_odds, e.home_team, e.away_team, e.start_time, oc.last_seen
            from offers_current oc join events e on e.id = oc.event_id
            where e.start_time > now() and oc.last_seen > now() - make_interval(hours => %s)
            order by e.start_time, oc.player_name, oc.market, oc.book""", (max_age_hours,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def safe_schema_name(name):
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", name):
        raise ValueError(f"bad schema name {name!r}")
    return name
