"""Action Network parsing (spm/collector.py) against a recorded real response.

The fixture is a trimmed copy of a real /v2/games/{id}/props response for one
opening-night game (points market, 4 players), plus the book-id names it uses.
"""

import json
from pathlib import Path

import pytest

from spm import collector

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def payload():
    return json.loads((FIXTURES / "an_points_game.json").read_text())


@pytest.fixture(scope="module")
def books():
    return {int(k): v for k, v in json.loads((FIXTURES / "an_books.json").read_text()).items()}


@pytest.fixture(scope="module")
def rows(payload, books):
    return collector.parse_an_props(payload, books)


def test_parses_every_player(rows):
    assert {r[0] for r in rows} == {"Paul George", "Jayson Tatum", "John Collins", "Derrick White"}


def test_rows_are_complete_points_offers(rows):
    for name, stat, book, line, over, under in rows:
        assert stat == "PTS"
        assert line % 1 == 0.5 or line % 1 == 0.0
        assert isinstance(over, int) and isinstance(under, int)
        assert over <= -100 or over >= 100
        assert under <= -100 or under >= 100


def test_known_line(rows):
    # Recorded 2026-10-19: Paul George points at DraftKings, 15.5 -117 / -109
    assert ("Paul George", "PTS", "DraftKings", 15.5, -117, -109) in rows


def test_book_names_are_normalized(rows):
    names = {r[2] for r in rows}
    assert "DraftKings" in names and "FanDuel" in names
    assert not any(n.endswith(" NJ") for n in names)


def test_reference_lines_and_exchanges_excluded(rows):
    names = {r[2] for r in rows}
    assert not names & {"Consensus", "Open", "Polymarket"}


@pytest.mark.parametrize("raw, clean", [
    ("DK NJ", "DraftKings"), ("FanDuel NJ", "FanDuel"), ("Caesars NJ", "Caesars"),
    ("BetMGM", "BetMGM"), ("MGM PA", "BetMGM"),
])
def test_book_name(raw, clean):
    assert collector._book_name(raw) == clean


def _payload(outcomes):
    return {
        "players": [{"id": 1, "full_name": "Test Player"}],
        "player_props": {"core_bet_type_26_assists": [{"player_id": 1, "lines": {"68": outcomes}}]},
    }


def _o(side, odds, value, **extra):
    return {"side": side, "odds": odds, "value": value, "player_id": 1, **extra}


def test_needs_both_sides():
    assert collector.parse_an_props(_payload([_o("over", -110, 5.5)]), {68: "DK NJ"}) == []


def test_alt_and_live_lines_skipped():
    outcomes = [_o("over", -110, 5.5), _o("under", -110, 5.5),
                _o("over", 200, 8.5, is_alt_market=True), _o("under", -300, 8.5, is_alt_market=True),
                _o("over", -150, 4.5, is_live=True), _o("under", 120, 4.5, is_live=True)]
    assert collector.parse_an_props(_payload(outcomes), {68: "DK NJ"}) == [
        ("Test Player", "AST", "DraftKings", 5.5, -110, -110)]


def test_unknown_market_ignored():
    p = _payload([_o("over", -110, 5.5), _o("under", -110, 5.5)])
    p["player_props"] = {"core_bet_type_999_unknown": p["player_props"]["core_bet_type_26_assists"]}
    assert collector.parse_an_props(p, {68: "DK NJ"}) == []
