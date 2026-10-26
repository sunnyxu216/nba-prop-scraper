"""MotherDuck archive (spm/store/archive.py), with a local DuckDB file standing in for MotherDuck.

Postgres side: a throwaway schema (TEST_DATABASE_URL, or DATABASE_URL from .env), dropped afterwards.
DuckDB reads it through the postgres extension (downloaded on first use).
"""

import datetime as dt
import os
import uuid

import pytest

from spm import envfile, odds
from spm.odds import Offer

envfile.load()
URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="no TEST_DATABASE_URL / DATABASE_URL configured")
duckdb = pytest.importorskip("duckdb")

UTC = dt.timezone.utc
TIP = dt.datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
NOW = TIP + dt.timedelta(days=1)


@pytest.fixture
def env(tmp_path):
    from spm.store import archive, pg
    schema = pg.safe_schema_name(f"test_{uuid.uuid4().hex[:12]}")
    conn = pg.connect(URL, schema=schema)
    pg.migrate(conn)
    pg.sync_sources(conn, odds.registered())
    con = archive.connect(URL, str(tmp_path / "archive.duckdb"))
    yield conn, con, schema
    con.close()
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(f'drop schema "{schema}" cascade')
    conn.commit()
    conn.close()


def offer(event="g1", name="Jayson Tatum", line=24.5, observed="2026-10-20T20:00:00Z", tip=TIP):
    return Offer(source="action_network", sport="nba", event_id=event, start_time=tip.isoformat(),
                 home_team="DET", away_team="BOS", player_name=name, source_player_id="", team="BOS", market="PTS",
                 book="DraftKings", line=line, over_odds=-110, under_odds=-110, observed_at=observed)


def seed(conn, event="g1", tip=TIP, actual=30):
    """Two price moves and one grade for one game."""
    from spm.store import pg
    pg.record(conn, [offer(event, tip=tip)])
    pg.record(conn, [offer(event, line=25.5, observed=(tip - dt.timedelta(hours=1)).isoformat(), tip=tip)])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("select id from events where source_event_id = %s", (event,))
        eid = cur.fetchone()[0]
        cur.execute("""insert into grades (event_id, player_key, market, book, sport, source, player_name, line,
                       over_odds, under_odds, closing_observed_at, actual, result, graded_at)
                       values (%s, 'jayson tatum', 'PTS', 'DraftKings', 'nba', 'action_network', 'Jayson Tatum',
                       25.5, -110, -110, %s, %s, 'over', %s)""",
                    (eid, tip - dt.timedelta(hours=1), actual, tip + dt.timedelta(hours=10)))
    return eid


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {table}")
        return cur.fetchone()[0]


def test_copy_verifies_and_keeps_source(env):
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    assert archive.pending(conn, now=NOW) == [eid]
    copied = archive.copy(con, conn, [eid], schema, now=NOW)
    assert copied == {eid: {"offers": 2, "grades": 1, "predictions": 0}}
    assert con.execute("select distinct source, sport from main.offers").fetchall() == [("action_network", "nba")]
    assert archive.pending(conn, now=NOW) == []                         # nothing new since


def test_copy_is_idempotent_and_picks_up_regrades(env):
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    archive.copy(con, conn, [eid], schema, now=NOW)
    archive.copy(con, conn, [eid], schema, now=NOW)
    assert con.execute("select count(*) from main.offers").fetchone()[0] == 2     # replaced, not duplicated
    with conn.transaction(), conn.cursor() as cur:                     # a stat correction regrades the game
        cur.execute("update grades set actual = 20, result = 'under', graded_at = %s", (NOW + dt.timedelta(hours=1),))
    assert archive.pending(conn, now=NOW + dt.timedelta(hours=2)) == [eid]
    archive.copy(con, conn, [eid], schema, now=NOW + dt.timedelta(hours=2))
    assert con.execute("select result from main.grades").fetchall() == [("under",)]


def test_unfinished_games_wait(env):
    from spm.store import archive
    conn, _, _ = env
    seed(conn, tip=NOW - dt.timedelta(hours=2))                          # started 2h ago: grading not run yet
    assert archive.pending(conn, now=NOW) == []


def test_delete_only_after_hot_window_and_verification(env):
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    archive.copy(con, conn, [eid], schema, now=NOW)
    assert archive.deletable(conn, now=NOW) == []                       # inside the 7-day window
    later = TIP + dt.timedelta(days=archive.HOT_DAYS + 1)
    assert archive.deletable(conn, now=later) == [eid]
    gone = archive.delete(conn, archive.deletable(conn, now=later), now=later)
    assert gone["offers"] == 2 and gone["grades"] == 1
    assert (count(conn, "offers"), count(conn, "grades"), count(conn, "events")) == (0, 0, 1)   # events stay
    assert con.execute("select count(*) from main.offers").fetchone()[0] == 2                    # archive keeps all
    assert archive.pending(conn, now=later) == []                       # deleted games aren't re-copied


def test_rows_added_after_archiving_block_deletion(env):
    from spm.store import archive, pg
    conn, con, schema = env
    eid = seed(conn)
    archive.copy(con, conn, [eid], schema, now=NOW)
    pg.record(conn, [offer(line=26.5, observed=(TIP - dt.timedelta(minutes=30)).isoformat())])   # not archived
    assert archive.deletable(conn, now=TIP + dt.timedelta(days=archive.HOT_DAYS + 1)) == []


def test_ungraded_games_are_never_deleted(env):
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from grades")
    archive.copy(con, conn, [eid], schema, now=NOW)
    assert archive.deletable(conn, now=TIP + dt.timedelta(days=archive.HOT_DAYS + 1)) == []


def test_paper_summary_runs_on_the_archive(env):
    """paper.summary()'s SQL runs unchanged against DuckDB through the adapter."""
    from spm.store import archive, paper
    conn, con, schema = env
    eid = seed(conn)
    archive.copy(con, conn, [eid], schema, now=NOW)
    con.execute("create table if not exists main.predictions as select * from pg.%s.predictions limit 0" % schema)
    s = paper.summary(archive.DuckConn(con), "nba")
    assert s["checkpoints"]["close"]["picks"] == 0                      # no predictions logged in this test
    assert archive.paper_summary(conn, "nba")["scope"] == "recent"      # nothing stored yet: live fallback


def test_health_flags_unarchived_old_games(env):
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    assert archive.problems(conn, now=TIP + dt.timedelta(days=3))
    archive.copy(con, conn, [eid], schema, now=NOW)
    assert archive.problems(conn, now=TIP + dt.timedelta(days=3)) == []


def test_box_scores_and_artifacts_are_never_deleted(env):
    """The NBA scorer rebuilds this season's game logs from `results`; `artifacts` is the NBA model bundle."""
    from spm.store import archive
    conn, con, schema = env
    eid = seed(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""insert into results (sport, game_date, source_game_id, player_key, player_name, team, opponent,
                       minutes, pts, reb, ast, fg3m, stl, blk, tov)
                       values ('nba', '2026-10-20', 'espn:1', 'jayson tatum', 'Jayson Tatum', 'BOS', 'DET', 36,
                       30, 8, 5, 3, 1, 0, 2)""")
    archive.copy(con, conn, [eid], schema, now=NOW)
    later = TIP + dt.timedelta(days=archive.HOT_DAYS + 30)
    archive.delete(conn, archive.deletable(conn, now=later), now=later)
    assert count(conn, "results") == 1                                 # kept in Supabase
    assert con.execute("select count(*) from main.results").fetchone()[0] == 1       # and archived
    assert "artifacts" not in archive.BY_EVENT + archive.RESULTS
