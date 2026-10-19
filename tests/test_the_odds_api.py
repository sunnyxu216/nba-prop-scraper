"""The Odds API adapter parsing (spm/odds/the_odds_api.py).

The fixture is hand-built from the v4 docs because there's no key yet; swap in
a recorded response once there is one.
"""

import json
from pathlib import Path

import pytest

from spm.odds import the_odds_api as toa

FIXTURES = Path(__file__).parent / "fixtures"
EVENT = json.loads((FIXTURES / "odds_api_events.json").read_text())[0]


@pytest.fixture(scope="module")
def offers():
    odds = json.loads((FIXTURES / "odds_api_event_odds.json").read_text())
    return toa.parse_event_odds(odds, EVENT, "2026-10-19T14:00:00Z")


def key(o):
    return (o.player_name, o.market, o.book, o.line, o.over_odds, o.under_odds)


def test_parses_paired_outcomes(offers):
    assert sorted(key(o) for o in offers) == sorted([
        ("Jayson Tatum", "PTS", "DraftKings", 26.5, -115, -105),
        ("Jayson Tatum", "3PM", "DraftKings", 3.5, 120, -150),
        ("Jayson Tatum", "PTS", "FanDuel", 27.5, -120, -102),
    ])


def test_unpaired_and_unknown_markets_skipped(offers):
    names = {o.player_name for o in offers}
    assert "Cade Cunningham" not in names          # over only, no under
    assert all(o.market != "DD" for o in offers)    # double-double market isn't mapped


def test_team_names_become_codes(offers):
    assert {(o.home_team, o.away_team) for o in offers} == {("DET", "BOS")}


def test_licensed_and_needs_key():
    assert toa.TheOddsAPI.licensed is True
    assert not toa.TheOddsAPI(cfg={**toa.DEFAULT_CONFIG, "odds_api_key": ""}).available()
    assert toa.TheOddsAPI(cfg={**toa.DEFAULT_CONFIG, "odds_api_key": "x"}).available()


# --------------------------------------------------------------------------- NFL (also hand-built)

@pytest.fixture(scope="module")
def nfl_offers():
    event = json.loads((FIXTURES / "odds_api_nfl_events.json").read_text())[0]
    odds = json.loads((FIXTURES / "odds_api_nfl_event_odds.json").read_text())
    return toa.parse_event_odds(odds, event, "2026-10-19T14:00:00Z", sport="nfl")


def test_nfl_markets_and_one_sided_anytime_td(nfl_offers):
    assert sorted(key(o) for o in nfl_offers) == sorted([
        ("Caleb Williams", "PASS_YDS", "DraftKings", 231.5, -115, -105),
        ("Stefon Diggs", "REC", "DraftKings", 4.5, 120, -150),
        ("D'Andre Swift", "ANYTIME_TD", "DraftKings", 0.5, 140, None),          # no "No" price posted
        ("Rhamondre Stevenson", "ANYTIME_TD", "DraftKings", 0.5, 165, -210),
    ])                                                                          # DJ Moore: over only; kicking unmapped


def test_nfl_team_codes(nfl_offers):
    assert {(o.sport, o.home_team, o.away_team) for o in nfl_offers} == {("nfl", "CHI", "NE")}
