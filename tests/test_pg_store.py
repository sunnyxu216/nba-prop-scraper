"""Postgres storage (spm/store/pg.py): migrations, change-only writes, idempotency, licensing view.

Runs against TEST_DATABASE_URL, or DATABASE_URL from .env, inside a throwaway
schema that is dropped afterwards, so it never touches real tables. Skipped
when no database is configured.
"""

import os
import uuid

import pytest

from spm import envfile, odds
from spm.odds import Offer

envfile.load()
URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="no TEST_DATABASE_URL / DATABASE_URL configured")

if URL:
    from spm.store import pg


@pytest.fixture
def conn():
    schema = pg.safe_schema_name(f"test_{uuid.uuid4().hex[:12]}")
    c = pg.connect(URL, schema=schema)
    pg.migrate(c)
    pg.sync_sources(c, odds.registered())
    yield c
    c.rollback()
    with c.cursor() as cur:
        cur.execute(f'drop schema "{schema}" cascade')
    c.commit()
    c.close()


def offer(line=24.5, over=-110, under=-110, observed="2026-10-20T12:00:00Z", book="DraftKings",
          source="action_network", event="1", name="Jayson Tatum"):
    return Offer(source=source, sport="nba", event_id=event, start_time="2026-10-20T23:00:00Z",
                 home_team="DET", away_team="BOS", player_name=name, source_player_id="99", team="BOS",
                 market="PTS", book=book, line=line, over_odds=over, under_odds=under, observed_at=observed)


def q(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_migrations_apply_once(conn):
    assert pg.migrate(conn) == []
    versions = [v for (v,) in q(conn, "select version from schema_migrations order by version")]
    assert versions[0] == "001_init" and versions == sorted(set(versions))


def test_first_snapshot_appends_everything(conn):
    assert pg.record(conn, [offer(), offer(book="FanDuel")]) == 2
    assert q(conn, "select count(*) from offers") == [(2,)]
    assert q(conn, "select count(*) from offers_current") == [(2,)]
    assert q(conn, "select count(*) from events") == [(1,)]


def test_rerunning_same_snapshot_is_a_no_op(conn):
    pg.record(conn, [offer()])
    assert pg.record(conn, [offer()]) == 0
    assert q(conn, "select count(*) from offers") == [(1,)]


def test_unchanged_price_only_updates_last_seen(conn):
    pg.record(conn, [offer()])
    assert pg.record(conn, [offer(observed="2026-10-20T13:00:00Z")]) == 0
    (changed, last), = q(conn, "select changed_at::text, last_seen::text from offers_current")
    assert changed.startswith("2026-10-20 12:00") and last.startswith("2026-10-20 13:00")


def test_moves_are_appended_and_current_updated(conn):
    pg.record(conn, [offer()])
    assert pg.record(conn, [offer(over=-125, under=105, observed="2026-10-20T13:00:00Z")]) == 1
    assert pg.record(conn, [offer(line=25.5, observed="2026-10-20T14:00:00Z")]) == 1
    assert q(conn, "select line::float, over_odds from offers order by observed_at") == [
        (24.5, -110), (24.5, -125), (25.5, -110)]
    assert q(conn, "select line::float, changed_at::text like '2026-10-20 14:00%' from offers_current") == [(25.5, True)]


def test_player_key_matches_across_spellings(conn):
    pg.record(conn, [offer(name="Nikola Jokić")])
    assert pg.record(conn, [offer(name="Nikola Jokic", observed="2026-10-20T13:00:00Z")]) == 0
    assert q(conn, "select player_key from offers_current") == [("nikola jokic",)]


def test_duplicates_within_a_snapshot_collapse(conn):
    assert pg.record(conn, [offer(), offer()]) == 1


def test_licensed_view_excludes_scraped_sources(conn):
    pg.record(conn, [offer(), offer(source="the_odds_api", event="e1")])
    assert q(conn, "select source from offers_licensed") == [("the_odds_api",)]
    assert q(conn, "select name, licensed from sources order by name") == [
        ("action_network", False), ("the_odds_api", True)]


def test_unknown_sport_rejected(conn):
    bad = offer()
    bad = Offer(**{**bad.as_dict(), "sport": "nbaa"})
    with pytest.raises(Exception):
        pg.record(conn, [bad])
