"""Pipeline health checks (spm/store/health.py)."""

import datetime as dt
import os
import uuid

import pytest

from spm import envfile, odds
from spm.odds import Offer
from spm.store import health

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


def st(last_seen_hours_ago=1, upcoming=3, ungraded=0):
    last = None if last_seen_hours_ago is None else NOW - dt.timedelta(hours=last_seen_hours_ago)
    return {"now": NOW, "last_seen": last, "upcoming_games": upcoming, "ungraded_old_games": ungraded}


def test_healthy():
    assert health.problems(st()) == []


def test_stale_collection_with_games_coming():
    (p,) = health.problems(st(last_seen_hours_ago=5))
    assert p.startswith("Collection is stale") and "5.0h ago" in p


def test_never_collected():
    assert "never" in health.problems(st(last_seen_hours_ago=None))[0]


def test_no_games_means_quiet_is_fine():
    assert health.problems(st(last_seen_hours_ago=48, upcoming=0)) == []   # off days / All-Star break


def test_grading_backlog():
    (p,) = health.problems(st(ungraded=2))
    assert p.startswith("Grading is behind: 2 games")


# --------------------------------------------------------------------------- database

envfile.load()
URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="no TEST_DATABASE_URL / DATABASE_URL configured")


@pytest.fixture
def conn():
    from spm.store import pg
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


def offer(tip, observed, event):
    return Offer(source="action_network", sport="nba", event_id=event, start_time=tip, home_team="DET",
                 away_team="BOS", player_name="Jayson Tatum", source_player_id="", team="BOS", market="PTS",
                 book="DraftKings", line=24.5, over_odds=-110, under_odds=-110, observed_at=observed)


@needs_db
def test_status_from_database(conn):
    from spm.store import pg
    pg.record(conn, [offer("2026-10-21T00:00:00Z", "2026-10-20T16:00:00Z", "tonight")])     # upcoming game
    pg.record(conn, [offer("2026-10-17T23:00:00Z", "2026-10-17T20:00:00Z", "last-week")])   # finished, ungraded
    s = health.status(conn, now=NOW)
    assert s["upcoming_games"] == 1 and s["ungraded_old_games"] == 1
    assert s["last_seen"] == dt.datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
    assert health.problems(s) == [p for p in health.problems(s) if p.startswith("Grading is behind")]
    assert "Weekly health report" in health.weekly_report(conn, now=NOW)


@needs_db
def test_status_per_sport(conn):
    from spm.store import pg
    pg.record(conn, [offer("2026-10-21T00:00:00Z", "2026-10-20T16:00:00Z", "tonight")])       # NBA, fresh
    nfl = Offer(**{**offer("2026-10-21T00:15:00Z", "2026-10-20T08:00:00Z", "mnf").as_dict(),   # NFL, 10h old
                   "sport": "nfl", "home_team": "SF", "away_team": "WAS", "team": "WAS", "market": "REC"})
    pg.record(conn, [nfl])
    nba, nfl_s = health.status(conn, now=NOW, sport="nba"), health.status(conn, now=NOW, sport="nfl")
    assert health.problems(nba) == []
    (p,) = health.problems(nfl_s)                       # NBA's fresh lines no longer hide NFL going stale
    assert p.startswith("Collection is stale")
    old = Offer(**{**nfl.as_dict(), "event_id": "last-week", "start_time": "2026-10-13T00:15:00Z",
                   "observed_at": "2026-10-12T20:00:00Z"})
    pg.record(conn, [old])
    assert health.status(conn, now=NOW, sport="nfl", check_grading=False)["ungraded_old_games"] == 0
    assert health.status(conn, now=NOW, sport="nfl")["ungraded_old_games"] == 1
