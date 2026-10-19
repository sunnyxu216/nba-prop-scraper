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
from spm.odds import action_network as an
from spm.odds import the_odds_api as toa

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def an_http(fail_market=None):
    """Fake Action Network: serves today's scoreboard and the recorded points market."""
    books = load("an_books.json")
    today = dt.date.today().strftime("%Y%m%d")

    def http(url, params=None, headers=None):
        params = params or {}
        if url.endswith("/v1/books"):
            return {"books": [{"id": int(k), "display_name": v} for k, v in books.items()]}
        if url.endswith("/v1/scoreboard/nba"):
            return load("an_scoreboard.json") if params.get("date") == today else {"games": []}
        if url.endswith("/v2/games/295136/props"):
            if params.get("customPickType") == fail_market:
                raise RuntimeError("HTTP 503")
            if params.get("customPickType") == "core_bet_type_27_points":
                return load("an_points_game.json")
            return {"player_props": {}}
        raise AssertionError(f"unexpected request {url}")
    return http


def toa_http(url, params=None, headers=None):
    if url.endswith("/events"):
        return load("odds_api_events.json")
    if url.endswith("/events/e1/odds"):
        return load("odds_api_event_odds.json")
    raise AssertionError(f"unexpected request {url}")


ADAPTERS = {
    "action_network": lambda: an.ActionNetwork(http=an_http(), delay=0),
    "the_odds_api": lambda: toa.TheOddsAPI(http=toa_http, cfg={**toa.DEFAULT_CONFIG, "odds_api_key": "test"}),
}


def test_every_registered_source_has_a_contract_test():
    assert set(ADAPTERS) == set(odds.registered())


@pytest.fixture(params=sorted(ADAPTERS))
def fetched(request):
    source = ADAPTERS[request.param]()
    return source, source.fetch("nba", days=2)


def parse_utc(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_returns_offers(fetched):
    source, offers = fetched
    assert offers and all(isinstance(o, odds.Offer) for o in offers)


def test_offer_fields(fetched):
    source, offers = fetched
    for o in offers:
        assert o.source == source.name and o.sport == "nba"
        assert o.market in odds.MARKETS
        assert o.player_name and o.book and o.event_id
        assert o.home_team in TEAMS and o.away_team in TEAMS
        assert o.team in ("", o.home_team, o.away_team)
        assert isinstance(o.line, float) and (o.line * 2).is_integer()
        for price in (o.over_odds, o.under_odds):
            assert isinstance(price, int) and (price <= -100 or price >= 100)
        assert parse_utc(o.start_time).tzinfo and parse_utc(o.observed_at).tzinfo


def test_no_duplicate_offers(fetched):
    _, offers = fetched
    keys = [(o.event_id, o.player_name, o.market, o.book, o.line) for o in offers]
    assert len(keys) == len(set(keys))


def test_unknown_sport_rejected():
    for make in ADAPTERS.values():
        with pytest.raises(ValueError):
            make().fetch("curling")


def test_partial_failure_keeps_other_markets():
    source = an.ActionNetwork(http=an_http(fail_market="core_bet_type_23_rebounds"), delay=0)
    offers = source.fetch("nba", days=1)
    assert offers and source.errors and "core_bet_type_23_rebounds" in source.errors[0]


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
