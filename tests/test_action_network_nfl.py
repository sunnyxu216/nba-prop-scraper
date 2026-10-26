"""Action Network NFL parsing against recorded real responses.

Fixtures are trimmed copies of responses recorded 2026-10-19 for WAS @ SF (Week 6,
Monday night): passing yards (2 QBs), receptions (3 players) and Anytime TD
(4 players), plus the Week 6 and Week 7 scoreboards (2 games each).
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from spm.odds import action_network as an

FIXTURES = Path(__file__).parent / "fixtures"
UTC = dt.timezone.utc


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def offers():
    books = {int(k): v for k, v in load("an_nfl_books.json").items()}
    game = an.parse_games(load("an_nfl_scoreboard.json"), "nfl")[-1]
    return [o for payload in load("an_nfl_props_game.json").values()
            for o in an.parse_props(payload, books, game, "2026-10-19T17:00:00Z", "nfl")]


def pick(offers, player, market, book="DraftKings"):
    (o,) = [o for o in offers if o.player_name == player and o.market == market and o.book == book]
    return o


def test_known_over_under_lines(offers):
    o = pick(offers, "Brock Purdy", "PASS_YDS")
    assert (o.line, o.over_odds, o.under_odds, o.team) == (252.5, -112, -112, "SF")
    o = pick(offers, "Stefon Diggs", "REC")
    assert (o.line, o.over_odds, o.under_odds, o.team) == (4.5, 127, -163, "WAS")


def test_anytime_td_is_one_sided_over_half(offers):
    o = pick(offers, "Mike Evans", "ANYTIME_TD")
    assert (o.line, o.over_odds, o.under_odds) == (0.5, -115, None)
    assert all(o.line == 0.5 and o.under_odds is None for o in offers if o.market == "ANYTIME_TD")


def test_sport_and_canonical_teams(offers):
    assert {(o.sport, o.home_team, o.away_team) for o in offers} == {("nfl", "SF", "WAS")}


def test_reference_books_excluded(offers):
    assert not {o.book for o in offers} & {"Consensus", "Polymarket"}


def test_game_teams_use_canonical_codes():
    games = an.parse_games({"games": [{"id": 1, "start_time": "2026-10-25T17:00:00.000Z", "type": "reg",
                                       "week": 7, "home_team_id": 2, "away_team_id": 3,
                                       "teams": [{"id": 2, "abbr": "JAC"}, {"id": 3, "abbr": "LA"}]}]}, "nfl")
    assert (games[0]["home"], games[0]["away"]) == ("JAX", "LA")


def board_http(calls):
    def http(url, params=None, headers=None):
        calls.append((url, params))
        if url.endswith("/v1/scoreboard/nfl"):
            return load("an_nfl_scoreboard_week7.json") if (params or {}).get("week") == 7 \
                else load("an_nfl_scoreboard.json")
        raise AssertionError(url)
    return http


def test_nfl_games_spans_current_and_next_week():
    calls = []
    src = an.ActionNetwork(http=board_http(calls), delay=0,
                           now=lambda: dt.datetime(2026, 10, 19, 18, 0, tzinfo=UTC))
    games = src.nfl_games(days=7)
    # the finished Thursday game is dropped; tonight's game and Week 7's two games are kept
    assert [g["id"] for g in games] == [290929, 290930, 290822]
    assert calls[1][1] == {"week": 7}


def test_nfl_games_respects_horizon_and_kickoff():
    src = an.ActionNetwork(http=board_http([]), delay=0,
                           now=lambda: dt.datetime(2026, 10, 20, 1, 0, tzinfo=UTC))  # after Monday kickoff
    assert [g["id"] for g in src.nfl_games(days=3)] == [290930]


@pytest.mark.parametrize("raw, clean", [("UnibetNJ", "Unibet"), ("HardRock NJ", "Hard Rock"),
                                        ("bet365 NJ", "bet365"), ("BetMGM NJ", "BetMGM")])
def test_book_name_nfl_books(raw, clean):
    assert an.book_name(raw) == clean


def fetch_http(calls):
    """Recorded NFL scoreboards + WAS @ SF props; counts every request."""
    props, books = load("an_nfl_props_game.json"), load("an_nfl_books.json")
    def http(url, params=None, headers=None):
        params = params or {}
        calls.append(url.rsplit("/web", 1)[-1])
        if url.endswith("/v1/scoreboard/nfl"):
            return load("an_nfl_scoreboard_week7.json") if params.get("week") == 7 else load("an_nfl_scoreboard.json")
        if url.endswith("/v1/books"):
            return {"books": [{"id": int(k), "display_name": v} for k, v in books.items()]}
        if url.endswith("/v2/games/290929/props"):
            return props.get(params.get("customPickType"), {"player_props": {}})
        if "/props" in url:
            return {"player_props": {}}
        raise AssertionError(url)
    return http


def test_starting_within_fetches_only_imminent_games():
    calls = []
    src = an.ActionNetwork(http=fetch_http(calls), delay=0,
                           now=lambda: dt.datetime(2026, 10, 19, 23, 45, tzinfo=UTC))   # 30 min before WAS @ SF
    src.starting_within = dt.timedelta(minutes=45)
    offers = src.fetch("nfl", days=7)
    assert offers and {o.event_id for o in offers} == {"290929"}
    assert not any("/v2/games/290930/" in c for c in calls)            # Thursday's game isn't re-fetched


def test_starting_within_with_nothing_imminent_is_one_scoreboard_pass():
    calls = []
    src = an.ActionNetwork(http=fetch_http(calls), delay=0,
                           now=lambda: dt.datetime(2026, 10, 19, 18, 0, tzinfo=UTC))    # 6h before
    src.starting_within = dt.timedelta(minutes=45)
    assert src.fetch("nfl", days=7) == []
    assert all("scoreboard" in c for c in calls)                       # no books or props requests
