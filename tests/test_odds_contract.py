"""Contract tests: rules every odds source must follow, whichever API it wraps.

Each adapter runs its real `fetch()` against a fake HTTP function that serves
recorded/hand-built responses, so these exercise the full adapter without the
network. A new source only needs a fake_http entry below to be held to the
same contract.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from spm import odds
from spm.data import TEAMS
from spm.nfl import teams as nfl_teams
from spm.odds import action_network as an
from spm.odds import the_odds_api as toa

FIXTURES = Path(__file__).parent / "fixtures"
SPORT_TEAMS = {"nba": TEAMS, "nfl": nfl_teams.TEAMS}
NFL_NOW = dt.datetime(2026, 10, 19, 18, 0, tzinfo=dt.timezone.utc)   # Monday of Week 6 (recorded fixtures)
DAYS = {"nba": 2, "nfl": 7}


def load(name):
    return json.loads((FIXTURES / name).read_text())


def an_http(fail_market=None):
    """Fake Action Network: the recorded opening-night NBA scoreboard (Oct 20) + points market;
    the recorded NFL weeks + props."""
    books = load("an_books.json") | load("an_nfl_books.json")
    nfl_props = load("an_nfl_props_game.json")

    def http(url, params=None, headers=None):
        params = params or {}
        if url.endswith("/v1/books"):
            return {"books": [{"id": int(k), "display_name": v} for k, v in books.items()]}
        if url.endswith("/v1/scoreboard/nba"):
            return load("an_scoreboard.json") if params.get("date") == "20261020" else {"games": []}
        if url.endswith("/v1/scoreboard/nfl"):
            return load("an_nfl_scoreboard_week7.json" if params.get("week") == 7 else "an_nfl_scoreboard.json")
        if url.endswith("/v2/games/290929/props"):
            return nfl_props.get(params.get("customPickType"), {"player_props": {}})
        if url.endswith("/v2/games/295136/props"):
            if params.get("customPickType") == fail_market:
                raise RuntimeError("HTTP 503")
            if params.get("customPickType") == "core_bet_type_27_points":
                return load("an_points_game.json")
            return {"player_props": {}}
        if url.endswith("/props") and "/v2/games/" in url:
            return {"player_props": {}}            # the other NFL games: no props recorded
        raise AssertionError(f"unexpected request {url}")
    return http


def toa_http(url, params=None, headers=None):
    nfl = "/americanfootball_nfl/" in url
    if url.endswith("/events"):
        return load("odds_api_nfl_events.json" if nfl else "odds_api_events.json")
    if url.endswith("/events/e1/odds"):
        return load("odds_api_event_odds.json")
    if url.endswith("/events/n1/odds"):
        return load("odds_api_nfl_event_odds.json")
    raise AssertionError(f"unexpected request {url}")


ADAPTERS = {
    "action_network": lambda: an.ActionNetwork(http=an_http(), delay=0, now=lambda: NFL_NOW),
    "the_odds_api": lambda: toa.TheOddsAPI(http=toa_http, cfg={**toa.DEFAULT_CONFIG, "odds_api_key": "test"}),
}


def test_every_registered_source_has_a_contract_test():
    assert set(ADAPTERS) == set(odds.registered())


def test_every_source_supports_every_sport():
    for make in ADAPTERS.values():
        assert set(make().sports) == set(odds.MARKETS_BY_SPORT)


@pytest.fixture(params=[(name, sport) for name in sorted(ADAPTERS) for sport in DAYS])
def fetched(request):
    name, sport = request.param
    source = ADAPTERS[name]()
    return source, sport, source.fetch(sport, days=DAYS[sport])


def parse_utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_returns_offers(fetched):
    source, sport, offers = fetched
    assert offers and all(isinstance(o, odds.Offer) for o in offers)


def test_offer_fields(fetched):
    source, sport, offers = fetched
    for o in offers:
        assert o.source == source.name and o.sport == sport
        assert o.market in odds.MARKETS_BY_SPORT[sport]
        assert o.player_name and o.book and o.event_id
        assert o.home_team in SPORT_TEAMS[sport] and o.away_team in SPORT_TEAMS[sport]
        assert o.team in ("", o.home_team, o.away_team)
        assert isinstance(o.line, float) and (o.line * 2).is_integer()
        prices = (o.over_odds,) if o.under_odds is None else (o.over_odds, o.under_odds)
        assert o.under_odds is not None or o.market in odds.ONE_SIDED
        for price in prices:
            assert isinstance(price, int) and (price <= -100 or price >= 100)
        assert parse_utc(o.start_time).tzinfo and parse_utc(o.observed_at).tzinfo


def test_no_duplicate_offers(fetched):
    _, _, offers = fetched
    keys = [(o.event_id, o.player_name, o.market, o.book, o.line) for o in offers]
    assert len(keys) == len(set(keys))


def test_unknown_sport_rejected():
    for make in ADAPTERS.values():
        with pytest.raises(ValueError):
            make().fetch("curling")


def test_partial_failure_keeps_other_markets():
    source = an.ActionNetwork(http=an_http(fail_market="core_bet_type_23_rebounds"), delay=0, now=lambda: NFL_NOW)
    offers = source.fetch("nba", days=2)
    assert offers and source.errors and "core_bet_type_23_rebounds" in source.errors[0]


# BOS @ DET tips 2026-10-20 19:00 UTC; the scoreboard is listed under the Eastern date 20261020
@pytest.mark.parametrize("now, fetched", [
    (dt.datetime(2026, 10, 20, 14, 0, tzinfo=dt.timezone.utc), True),    # game day, morning
    (dt.datetime(2026, 10, 19, 23, 30, tzinfo=dt.timezone.utc), True),   # day before, after 00:00 UTC is near
    (dt.datetime(2026, 10, 20, 18, 59, tzinfo=dt.timezone.utc), True),   # a minute before tip
    (dt.datetime(2026, 10, 20, 19, 0, tzinfo=dt.timezone.utc), False),   # tipped off: lines are live now
])
def test_nba_games_use_eastern_date_and_skip_started(now, fetched):
    src = an.ActionNetwork(http=an_http(), delay=0, now=lambda: now)
    assert bool(src.nba_games(days=1 if now.day == 20 else 2)) == fetched


def test_late_games_kept_after_utc_midnight():
    """01:17 UTC is still the previous evening in the US: tonight's 9:30 PM ET game must be fetched."""
    seen = []
    board = {"games": [{"id": 1, "start_time": "2026-10-21T01:30:00.000Z", "type": "reg", "teams": [],
                        "home_team_id": None, "away_team_id": None}]}
    def http(url, params=None, headers=None):
        seen.append(params["date"])
        return board if params["date"] == "20261020" else {"games": []}
    src = an.ActionNetwork(http=http, delay=0, now=lambda: dt.datetime(2026, 10, 21, 1, 17, tzinfo=dt.timezone.utc))
    assert [g["id"] for g in src.nba_games(days=2)] == [1]
    assert seen == ["20261020", "20261021"]


def test_the_odds_api_respects_horizon():
    src = ADAPTERS["the_odds_api"]()
    src.fetch("nba", days=2)
    assert set(src.raw) == {"e1"}   # the 2030 event is beyond the 2-day window


# --------------------------------------------------------------------------- registry

def test_licensed_sources():
    assert odds.licensed_sources() == {"the_odds_api"}


def test_enabled_sources_from_env(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCES", "action_network")
    assert [s.name for s in odds.enabled_sources()] == ["action_network"]


def test_unknown_source_rejected(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCES", "action_network,bogus")
    with pytest.raises(ValueError, match="bogus"):
        odds.enabled_sources()


def test_unavailable_sources_skipped(monkeypatch):
    monkeypatch.setenv("ODDS_SOURCES", "the_odds_api")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr(toa, "config", lambda: {**toa.DEFAULT_CONFIG, "odds_api_key": ""})
    assert odds.enabled_sources() == []
