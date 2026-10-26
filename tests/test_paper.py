"""Paper trading (spm/store/paper.py): checkpoints, logging, settlement and CLV.

Database tests run in a throwaway schema (TEST_DATABASE_URL, or DATABASE_URL from .env).
"""

import datetime as dt
import os
import uuid

import pytest

from spm import envfile, odds
from spm.odds import Offer
from spm.store import paper

UTC = dt.timezone.utc
KICK = dt.datetime(2026, 10, 25, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize("hours, expected", [
    (72, ["open"]), (20, ["open", "t24"]), (2.0, ["open", "t2"]), (0.4, ["open", "close"]), (-0.1, []),
])
def test_checkpoints(hours, expected):
    assert paper.checkpoints(KICK, KICK - dt.timedelta(hours=hours)) == expected


def test_same_call_at_several_books_is_one_pick():
    pick = lambda book, ev, event=1, market="REC": {"event_id": event, "player_key": "jj", "market": market,
                                                     "book": book, "ev": ev}
    kept = paper.one_per_bet([pick("bet365", 0.05), pick("FanDuel", 0.09), pick("DraftKings", 0.07),
                              pick("bet365", 0.06, market="REC_YDS"), pick("bet365", 0.06, event=2)])
    assert sorted((r["event_id"], r["market"], r["book"]) for r in kept) == [
        (1, "REC", "FanDuel"), (1, "REC_YDS", "bet365"), (2, "REC", "bet365")]


def test_settle_at_own_line():
    assert paper._settle("OVER", 4.5, 5) == "win" and paper._settle("UNDER", 4.5, 5) == "loss"
    assert paper._settle("OVER", 5.0, 5) == "push" and paper._settle("OVER", 4.5, None) == "void"


def test_clv_is_fair_probability_movement_toward_the_pick():
    from spm import ev
    # took the over at +120 / -150 (fair over ~43%); it closed -150 / +120 (fair over ~57%)
    clv = paper._clv("OVER", 120, -150, -150, 120)
    assert abs(clv - (ev.no_vig(-150, 120)[0] - ev.no_vig(120, -150)[0])) < 1e-12 and clv > 0.1
    assert paper._clv("UNDER", 120, -150, -150, 120) < -0.1        # the same move is bad for an under
    assert paper._clv("OVER", -110, -110, -110, -110) == 0          # unchanged price: zero, not -vig
    assert paper._clv("OVER", 300, None, 300, None) is None         # yes-only: no fair price


@pytest.mark.parametrize("side, pick, close, expected", [
    ("OVER", 4.5, 5.5, 1), ("OVER", 4.5, 3.5, -1), ("UNDER", 4.5, 3.5, 1), ("UNDER", 4.5, 5.5, -1), ("OVER", 4.5, 4.5, 0),
])
def test_line_move(side, pick, close, expected):
    assert paper._line_move(side, pick, close) == expected


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


def result(decision="UNDER", call=None, p_over=0.35, track=False):
    call = call or decision
    return {"decision": decision, "model_call": call, "model_version": "test", "projection": 4.1, "p_over": p_over,
            "p_under": 1 - p_over, "p_push": 0.0, "market_prob_over": 0.48, "ev_over": -0.2, "ev_under": 0.12,
            "tier": "Lean" if decision != "PASS" else None, "stake_pct": 0.01, "track_only": track,
            "low_confidence": False, "team": "MIN", "opp": "IND", "position": "WR", "kind": "count",
            "outside_training": False, "one_sided": False, "injury_status": None,
            "reasoning": ["Projection 4.1 receptions vs a line of 5.5."]}


def setup_offer(conn, line=5.5, over=-150, under=120, observed="2026-10-23T12:00:00Z"):
    from spm.store import pg
    o = Offer(source="action_network", sport="nfl", event_id="g1", start_time=KICK.isoformat(), home_team="MIN",
              away_team="IND", player_name="Justin Jefferson", source_player_id="", team="MIN", market="REC",
              book="bet365", line=line, over_odds=over, under_odds=under, observed_at=observed)
    pg.record(conn, [o])
    with conn.cursor() as cur:
        cur.execute("select event_id, player_key, last_seen from offers_current")
        event_id, key, seen = cur.fetchone()
    return {"event_id": event_id, "source": "action_network", "player_key": key, "player_name": o.player_name,
            "team": "MIN", "market": "REC", "book": "bet365", "line": line, "over_odds": over, "under_odds": under,
            "last_seen": seen, "home_team": "MIN", "away_team": "IND", "start_time": KICK}


def rows(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


@needs_db
def test_record_checkpoints_and_latest(conn):
    o = setup_offer(conn)
    assert paper.record(conn, [o], [result()], now=KICK - dt.timedelta(hours=48)) == (1, 1)    # open
    assert paper.record(conn, [o], [result()], now=KICK - dt.timedelta(hours=20)) == (2, 1)    # open (kept) + t24
    paper.record(conn, [o], [result(p_over=0.40)], now=KICK - dt.timedelta(minutes=50))         # close
    paper.record(conn, [o], [result(p_over=0.30)], now=KICK - dt.timedelta(minutes=5))          # close, overwritten
    got = {cp: float(p) for cp, p in rows(conn, "select checkpoint, round(p_over::numeric, 2) from predictions")}
    assert got == {"open": 0.35, "t24": 0.35, "close": 0.30}
    assert rows(conn, "select count(*) from predictions_latest") == [(1,)]
    assert rows(conn, "select reasoning is not null from predictions where checkpoint = 'open'") == [(True,)]


@needs_db
def test_pass_rows_keep_no_reasoning_and_skips_ignored(conn):
    o = setup_offer(conn)
    paper.record(conn, [o, o], [result("PASS"), {"skipped": "no history"}], now=KICK - dt.timedelta(hours=48))
    assert rows(conn, "select decision, reasoning is null from predictions") == [("PASS", True)]


@needs_db
def test_summary_settles_at_pick_line_with_clv(conn):
    from spm.store import grading
    o = setup_offer(conn)                                       # pick: under 5.5 at +120
    paper.record(conn, [o], [result()], now=KICK - dt.timedelta(minutes=30))
    # closing line moved to the same 5.5 at -110 / -110, then he caught 4: under wins at +120
    with conn.cursor() as cur:
        cur.execute("""insert into grades (event_id, player_key, market, book, sport, source, player_name, line,
                       over_odds, under_odds, closing_observed_at, actual, result)
                       values (%s, %s, 'REC', 'bet365', 'nfl', 'action_network', 'Justin Jefferson', 5.5, -110, -110,
                       %s, 4, 'under')""", (o["event_id"], o["player_key"], KICK - dt.timedelta(minutes=20)))
    conn.commit()
    s = paper.summary(conn, "nfl")["checkpoints"]["close"]
    assert (s["picks"], s["wins"], s["losses"]) == (1, 1, 0)
    assert abs(s["units"] - 1.2) < 1e-9 and abs(s["roi"] - 1.2) < 1e-9
    # fair under at the pick (-150 / +120) ~43%, at a -110/-110 close 50%: +~7 points toward the pick
    from spm import ev
    assert abs(s["avg_clv"] - (0.5 - ev.no_vig(-150, 120)[1])) < 1e-9 and s["clv_n"] == 1
    assert grading                                              # (grades table comes from migration 002)


@needs_db
def test_track_only_picks_counted_separately(conn):
    o = setup_offer(conn)
    paper.record(conn, [o], [result("PASS", call="UNDER", track=True)], now=KICK - dt.timedelta(minutes=30))
    with conn.cursor() as cur:
        cur.execute("""insert into grades (event_id, player_key, market, book, sport, source, player_name, line,
                       over_odds, under_odds, closing_observed_at, actual, result)
                       values (%s, %s, 'REC', 'bet365', 'nfl', 'action_network', 'Justin Jefferson', 5.5, -150, 120,
                       %s, 7, 'over')""", (o["event_id"], o["player_key"], KICK - dt.timedelta(minutes=20)))
    conn.commit()
    s = paper.summary(conn, "nfl")
    assert s["checkpoints"]["close"]["picks"] == 0              # not a recommended bet
    assert (s["track_only"]["picks"], s["track_only"]["losses"]) == (1, 1)


@needs_db
def test_track_only_no_call_on_yes_only_market_is_not_a_pick(conn):
    """Anytime TD with only a 'yes' price: a model 'no' call can't be bet, so it isn't counted."""
    o = setup_offer(conn, under=None)
    o["market"] = "REC"
    paper.record(conn, [o], [result("PASS", call="UNDER", track=True)], now=KICK - dt.timedelta(minutes=30))
    with conn.cursor() as cur:
        cur.execute("""insert into grades (event_id, player_key, market, book, sport, source, player_name, line,
                       over_odds, under_odds, closing_observed_at, actual, result)
                       values (%s, %s, 'REC', 'bet365', 'nfl', 'action_network', 'Justin Jefferson', 5.5, -150, null,
                       %s, 7, 'over')""", (o["event_id"], o["player_key"], KICK - dt.timedelta(minutes=20)))
    conn.commit()
    assert paper.summary(conn, "nfl")["track_only"]["picks"] == 0
