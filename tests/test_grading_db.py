"""Incremental grading (spm/store/grading.py, spm/boxscores.py).

Pure functions run everywhere; database tests use a throwaway schema like
test_pg_store.py and skip without a database.
"""

import datetime as dt
import os
import uuid

import pytest

from spm import boxscores, envfile, odds
from spm.odds import Offer
from spm.store import grading

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- pure

@pytest.mark.parametrize("start, date", [
    ("2026-10-20T23:30:00+00:00", "2026-10-20"),   # 7:30pm EDT
    ("2026-10-21T02:30:00+00:00", "2026-10-20"),   # 10:30pm EDT, already the 21st in UTC
    ("2026-12-26T01:00:00+00:00", "2026-12-25"),   # 8pm EST on Christmas
    ("2026-10-25T19:00:00+00:00", "2026-10-25"),   # 3pm matinee
])
def test_et_date(start, date):
    assert grading.et_date(dt.datetime.fromisoformat(start)).isoformat() == date


def test_market_value_and_settle():
    box = dict(pts=25, reb=8, ast=5, fg3m=3, stl=1, blk=2, tov=4)
    assert grading.market_value("PRA", box) == 38
    assert grading.market_value("SB", box) == 3
    assert grading.settle(24.5, 25) == "over"
    assert grading.settle(25.5, 25) == "under"
    assert grading.settle(25.0, 25) == "push"


@pytest.mark.parametrize("key, box, expected", [
    ("nic claxton", ["nicolas claxton", "cam johnson"], "nicolas claxton"),
    ("nicolas claxton", ["nic claxton"], "nic claxton"),
    ("jalen williams", ["jaylin williams", "chet holmgren"], None),        # different people
    ("tj mcconnell", ["t j mcconnell"], "t j mcconnell"),                  # T.J. vs TJ: same player
    ("jay williams", ["jaylin williams", "jaylen williams"], None),        # ambiguous: refuse
])
def test_match_name(key, box, expected):
    assert grading.match_name(key, box) == expected


def test_boxscore_parsing():
    headers = ["GAME_ID", "GAME_DATE", "PLAYER_NAME", "TEAM_ABBREVIATION", "MATCHUP", "MIN",
               "PTS", "REB", "AST", "FG3M", "STL", "BLK", "TOV"]
    payload = {"resultSets": [{"headers": headers, "rowSet": [
        ["0022600001", "2026-10-20", "Nikola Jokić", "DEN", "DEN vs. OKC", 36, 30, 12, 10, 2, 1, 1, 3],
        ["0022600001", "2026-10-20", "Shai Gilgeous-Alexander", "OKC", "OKC @ DEN", "35:30", 32, 5, 6, 2, 2, 1, 2],
    ]}]}
    seen = {}

    def http(url, params):
        seen.update(params)
        return payload
    rows = boxscores.fetch(dt.date(2026, 10, 20), dt.date(2026, 10, 21), http=http, source="nba")
    assert seen["DateFrom"] == "10/20/2026" and seen["Season"] == "2026-27"
    jokic, sga = rows
    assert (jokic["player_key"], jokic["team"], jokic["opponent"], jokic["pts"]) == ("nikola jokic", "DEN", "OKC", 30)
    assert (sga["opponent"], sga["minutes"]) == ("DEN", 35.5)


def test_espn_boxscore_parsing():
    board = {"events": [
        {"id": "401", "competitions": [{"status": {"type": {"completed": True}},
                                        "competitors": [{"team": {"abbreviation": "GS"}},
                                                        {"team": {"abbreviation": "LAL"}}]}]},
        {"id": "402", "competitions": [{"status": {"type": {"completed": False}}, "competitors": []}]},
    ]}
    labels = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]
    summary = {"boxscore": {"players": [
        {"team": {"abbreviation": "GS"}, "statistics": [{"labels": labels, "athletes": [
            {"athlete": {"displayName": "Stephen Curry"}, "didNotPlay": False,
             "stats": ["34", "31", "10-21", "6-14", "5-5", "4", "7", "3", "2", "0", "1", "3", "2", "+9"]},
            {"athlete": {"displayName": "Injured Guy"}, "didNotPlay": True, "stats": []},
        ]}]},
        {"team": {"abbreviation": "LAL"}, "statistics": [{"labels": labels, "athletes": []}]},
    ]}}
    calls = []

    def http(url, params):
        calls.append(url.rsplit("/", 1)[1])
        return board if url.endswith("/scoreboard") else summary
    rows = boxscores.fetch(dt.date(2026, 10, 21), dt.date(2026, 10, 21), http=http, source="espn")
    assert calls == ["scoreboard", "summary"]              # unfinished game 402 not fetched
    assert len(rows) == 1                                  # the DNP is skipped
    r = rows[0]
    assert (r["player_key"], r["team"], r["opponent"], r["source_game_id"]) == ("stephen curry", "GSW", "LAL", "espn:401")
    assert (r["pts"], r["reb"], r["ast"], r["fg3m"], r["stl"], r["blk"], r["tov"]) == (31, 4, 7, 6, 2, 0, 3)


# --------------------------------------------------------------------------- database

envfile.load()
URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="no TEST_DATABASE_URL / DATABASE_URL configured")
TIP = "2026-10-20T23:30:00Z"
BEFORE, EARLIER, AFTER = "2026-10-20T20:00:00Z", "2026-10-20T15:00:00Z", "2026-10-21T00:30:00Z"
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


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


def offer(name="Jayson Tatum", market="PTS", line=24.5, observed=BEFORE, book="DraftKings", event="g1",
          home="DET", away="BOS", tip=TIP):
    return Offer(source="action_network", sport="nba", event_id=event, start_time=tip, home_team=home,
                 away_team=away, player_name=name, source_player_id="", team="BOS", market=market, book=book,
                 line=line, over_odds=-110, under_odds=-110, observed_at=observed)


def box(name, team="BOS", opp="DET", pts=0, reb=0, ast=0, game="0022600001", date="2026-10-20"):
    from spm.data import norm_name
    return {"sport": "nba", "game_date": dt.date.fromisoformat(date), "source_game_id": game,
            "player_key": norm_name(name), "player_name": name, "team": team, "opponent": opp,
            "minutes": 30.0, "pts": pts, "reb": reb, "ast": ast, "fg3m": 0, "stl": 0, "blk": 0, "tov": 0}


def grades(conn):
    with conn.cursor() as cur:
        cur.execute("select player_name, market, book, line::float, actual::float, result from grades "
                    "order by player_name, market, book")
        return cur.fetchall()


def run(conn):
    return grading.grade_events(conn, grading.pending_events(conn, now=NOW))


@needs_db
def test_closing_line_is_last_price_before_tip(conn):
    from spm.store import pg
    pg.record(conn, [offer(line=23.5, observed=EARLIER)])
    pg.record(conn, [offer(line=24.5, observed=BEFORE)])
    pg.record(conn, [offer(line=30.5, observed=AFTER)])        # after tip-off: ignored
    grading.upsert_results(conn, [box("Jayson Tatum", pts=25)])
    assert run(conn) == (1, 0, [])
    assert grades(conn) == [("Jayson Tatum", "PTS", "DraftKings", 24.5, 25.0, "over")]


@needs_db
def test_void_when_player_did_not_play(conn):
    from spm.store import pg
    pg.record(conn, [offer(), offer(name="Derrick White")])
    grading.upsert_results(conn, [box("Jayson Tatum", pts=20)])  # White has no box score row
    assert run(conn) == (1, 1, [])
    assert {(n, r) for n, _, _, _, _, r in grades(conn)} == {("Jayson Tatum", "under"), ("Derrick White", "void")}


@needs_db
def test_spelling_difference_is_not_a_dnp(conn):
    from spm.store import pg
    pg.record(conn, [offer(name="Nic Claxton")])
    grading.upsert_results(conn, [box("Nicolas Claxton", pts=26)])
    assert run(conn) == (1, 0, [])


@needs_db
def test_waits_for_box_score_then_grades(conn):
    from spm.store import pg
    pg.record(conn, [offer()])
    graded, voided, skipped = run(conn)
    assert (graded, voided, len(skipped)) == (0, 0, 1)          # no box score yet: retry later
    grading.upsert_results(conn, [box("Jayson Tatum", pts=25)])
    assert run(conn)[0] == 1


@needs_db
def test_second_run_is_a_no_op(conn):
    from spm.store import pg
    pg.record(conn, [offer(), offer(market="PRA", line=37.5)])
    grading.upsert_results(conn, [box("Jayson Tatum", pts=25, reb=8, ast=5)])
    assert run(conn) == (2, 0, [])
    assert grading.pending_events(conn, now=NOW) == []
    assert run(conn) == (0, 0, [])


@needs_db
def test_unfinished_games_not_pending(conn):
    from spm.store import pg
    pg.record(conn, [offer()])
    assert grading.pending_events(conn, now=dt.datetime(2026, 10, 21, 0, 0, tzinfo=UTC)) == []  # tip + 30 min


@needs_db
def test_regrade_after_stat_correction(conn):
    from spm.store import pg
    pg.record(conn, [offer()])
    grading.upsert_results(conn, [box("Jayson Tatum", pts=24)])
    run(conn)
    assert grades(conn)[0][-1] == "under"
    grading.upsert_results(conn, [box("Jayson Tatum", pts=25)])   # stat correction
    assert grading.delete_grades_since(conn, dt.date(2026, 10, 20)) == 1
    run(conn)
    assert grades(conn)[0][-1] == "over"


@needs_db
def test_nfl_games_not_picked_up_by_nba_grader(conn):
    from spm.store import pg
    nfl = Offer(**{**offer().as_dict(), "sport": "nfl", "event_id": "n1", "home_team": "CHI", "away_team": "NE",
                   "market": "REC", "team": "NE"})
    pg.record(conn, [nfl])
    assert grading.pending_events(conn, now=NOW) == []
    assert [e[2:] for e in grading.pending_events(conn, now=NOW, sport="nfl")] == [("CHI", "NE")]


# --------------------------------------------------------------------------- NFL

NFL_TIP = "2026-10-18T13:30:00Z"   # JAX @ HOU in London (recorded fixture, tests/fixtures/espn_nfl_game.json)


def nfl_offer(name, market, line, team, over=-110, under=-110):
    return Offer(source="action_network", sport="nfl", event_id="jaxhou", start_time=NFL_TIP, home_team="HOU",
                 away_team="JAX", player_name=name, source_player_id="", team=team, market=market, book="DraftKings",
                 line=line, over_odds=over, under_odds=under, observed_at="2026-10-18T12:00:00Z")


def test_nfl_market_value():
    row = dict.fromkeys(grading.NFL_FIELDS, 0) | {"pass_td": 3, "rush_td": 1, "rec_yds": 40, "rush_yds": 25}
    assert grading.market_value("ANYTIME_TD", row, "nfl") == 1          # passing TDs don't count
    assert grading.market_value("RUSH_REC_YDS", row, "nfl") == 65
    assert set(grading.NFL_MARKETS) == set(odds.NFL_MARKETS)


@needs_db
def test_nfl_grading_end_to_end(conn):
    import json
    from pathlib import Path
    from spm.nfl import boxscores as nfl_box
    from spm.store import pg
    game = json.loads((Path(__file__).parent / "fixtures" / "espn_nfl_game.json").read_text())
    rows = nfl_box.parse_game(game["summary"], game["game_rosters"], game["names"], dt.date(2026, 10, 18))
    pg.record(conn, [
        nfl_offer("Trevor Lawrence", "PASS_YDS", 280.5, "JAX"),            # 306 -> over
        nfl_offer("Trevor Lawrence", "ANYTIME_TD", 0.5, "JAX", 600, None),  # no rushing TD -> under
        nfl_offer("Jaylin Noel", "REC", 4.5, "HOU"),                       # 4 -> under
        nfl_offer("Jaylin Noel", "ANYTIME_TD", 0.5, "HOU", 350, None),     # 1 rec TD -> over
        nfl_offer("Nico Collins", "RUSH_REC_YDS", 79.0, "HOU"),            # 5 + 74 = 79 -> push
        nfl_offer("David Montgomery", "RUSH_YDS", 55.5, "HOU"),            # didn't play -> void
    ])
    tip = dt.datetime(2026, 10, 18, 13, 30, tzinfo=UTC)
    assert grading.pending_events(conn, now=tip + dt.timedelta(hours=4), sport="nfl") == []   # may still be on
    events = grading.pending_events(conn, now=tip + dt.timedelta(hours=5), sport="nfl")
    assert grading.grade_events(conn, events, "nfl") == (0, 0, [events[0][0]])      # no box score loaded yet
    assert grading.upsert_results(conn, rows, "nfl") == len(rows)
    assert grading.grade_events(conn, events, "nfl") == (5, 1, [])
    assert grades(conn) == [
        ("David Montgomery", "RUSH_YDS", "DraftKings", 55.5, None, "void"),
        ("Jaylin Noel", "ANYTIME_TD", "DraftKings", 0.5, 1.0, "over"),
        ("Jaylin Noel", "REC", "DraftKings", 4.5, 4.0, "under"),
        ("Nico Collins", "RUSH_REC_YDS", "DraftKings", 79.0, 79.0, "push"),
        ("Trevor Lawrence", "ANYTIME_TD", "DraftKings", 0.5, 0.0, "under"),
        ("Trevor Lawrence", "PASS_YDS", "DraftKings", 280.5, 306.0, "over"),
    ]
    assert grading.upsert_results(conn, rows, "nfl") == len(rows)                    # re-load is an upsert
    assert grading.pending_events(conn, now=tip + dt.timedelta(hours=5), sport="nfl") == []
