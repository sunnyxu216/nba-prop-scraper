"""Action Network adapter parsing (spm/odds/action_network.py) against a recorded real response.

The fixture is a trimmed copy of a real /v2/games/{id}/props response for one
opening-night game (BOS @ DET, points market, 4 players), plus the book names it uses.
"""

import json
from pathlib import Path

import pytest

from spm.odds import action_network as an

FIXTURES = Path(__file__).parent / "fixtures"
GAME = {"id": 295136, "start": "2026-10-20T19:00:00.000Z", "home": "DET", "away": "BOS",
        "teams": {164: "BOS", 170: "DET"}}


@pytest.fixture(scope="module")
def offers():
    payload = json.loads((FIXTURES / "an_points_game.json").read_text())
    books = {int(k): v for k, v in json.loads((FIXTURES / "an_books.json").read_text()).items()}
    return an.parse_props(payload, books, GAME, "2026-10-19T14:00:00Z")


def test_parses_every_player(offers):
    assert {o.player_name for o in offers} == {"Paul George", "Jayson Tatum", "John Collins", "Derrick White"}


def test_known_line(offers):
    # Recorded 2026-10-19: Paul George points at DraftKings, 15.5 -117 / -109
    pg = [o for o in offers if o.player_name == "Paul George" and o.book == "DraftKings"]
    assert [(o.market, o.line, o.over_odds, o.under_odds) for o in pg] == [("PTS", 15.5, -117, -109)]


def test_player_team_from_source(offers):
    teams = {o.player_name: o.team for o in offers}
    assert teams["Paul George"] == "BOS" and teams["John Collins"] == "DET"


def test_event_fields_and_source(offers):
    o = offers[0]
    assert (o.source, o.sport, o.event_id, o.home_team, o.away_team) == ("action_network", "nba", "295136", "DET", "BOS")


def test_book_names_are_normalized(offers):
    names = {o.book for o in offers}
    assert {"DraftKings", "FanDuel"} <= names
    assert not any(n.endswith(" NJ") for n in names)


def test_reference_lines_and_exchanges_excluded(offers):
    assert not {o.book for o in offers} & {"Consensus", "Open", "Polymarket"}


@pytest.mark.parametrize("raw, clean", [
    ("DK NJ", "DraftKings"), ("FanDuel NJ", "FanDuel"), ("Caesars NJ", "Caesars"),
    ("BetMGM", "BetMGM"), ("MGM PA", "BetMGM"),
])
def test_book_name(raw, clean):
    assert an.book_name(raw) == clean


def _payload(outcomes, market="core_bet_type_26_assists"):
    return {"players": [{"id": 1, "full_name": "Test Player"}],
            "player_props": {market: [{"player_id": 1, "team_id": 164, "lines": {"68": outcomes}}]}}


def _o(side, odds, value, **extra):
    return {"side": side, "odds": odds, "value": value, "player_id": 1, **extra}


def _parse(payload):
    return an.parse_props(payload, {68: "DK NJ"}, GAME, "2026-10-19T14:00:00Z")


def test_needs_both_sides():
    assert _parse(_payload([_o("over", -110, 5.5)])) == []


def test_alt_and_live_lines_skipped():
    out = _parse(_payload([_o("over", -110, 5.5), _o("under", -110, 5.5),
                           _o("over", 200, 8.5, is_alt_market=True), _o("under", -300, 8.5, is_alt_market=True),
                           _o("over", -150, 4.5, is_live=True), _o("under", 120, 4.5, is_live=True)]))
    assert [(o.market, o.book, o.line, o.over_odds, o.under_odds) for o in out] == [("AST", "DraftKings", 5.5, -110, -110)]


def test_unknown_market_ignored():
    assert _parse(_payload([_o("over", -110, 5.5), _o("under", -110, 5.5)], market="core_bet_type_999_x")) == []
