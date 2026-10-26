"""The odds-source interface.

Every source (scraped or licensed) turns its own API format into a list of
`Offer`s. Nothing outside spm/odds/ knows which source an offer came from,
except through the `source` field, which is kept on every stored row so
unlicensed (scraped) data can always be excluded from commercial use.
"""

import datetime as dt
import os
from dataclasses import asdict, dataclass

# Canonical prop markets per sport. Adapters map their own market names onto these.
MARKETS = ("PTS", "REB", "AST", "3PM", "STL", "BLK", "TOV", "PRA", "PR", "PA", "RA", "SB")  # NBA
NFL_MARKETS = ("PASS_YDS", "PASS_CMP", "PASS_ATT", "PASS_TD", "INT", "RUSH_YDS", "RUSH_ATT",
               "REC", "REC_YDS", "RUSH_REC_YDS", "ANYTIME_TD")
MARKETS_BY_SPORT = {"nba": MARKETS, "nfl": NFL_MARKETS}
# Yes/no markets, stored as over 0.5 ("yes"); books often post no "no" price, so under_odds may be None.
ONE_SIDED = {"ANYTIME_TD"}


@dataclass(frozen=True)
class Offer:
    """One book's over/under price on one player prop, as observed at one moment."""
    source: str            # adapter name, e.g. "action_network"
    sport: str             # "nba", "nfl"
    event_id: str          # the source's own game id
    start_time: str        # tip-off / kickoff, UTC ISO 8601
    home_team: str         # team codes (NBA: as given by the source; NFL: spm/nfl/teams.py codes)
    away_team: str
    player_name: str
    source_player_id: str  # the source's own player id ("" if it has none)
    team: str              # player's team code if the source says, else ""
    market: str            # one of MARKETS_BY_SPORT[sport]
    book: str              # sportsbook display name, e.g. "DraftKings"
    line: float
    over_odds: int         # American odds
    under_odds: int | None  # None only for ONE_SIDED markets with no "no" price
    observed_at: str       # when this price was seen, UTC ISO 8601

    def as_dict(self):
        return asdict(self)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_http(url, params=None, headers=None):
    from ..data import _http_json
    return _http_json(url, params, headers=headers, retries=2, timeout=30)


class OddsSource:
    """Base class for odds adapters.

    Subclasses set `name` and `licensed`, and implement `fetch`. They receive an
    `http(url, params=None, headers=None) -> parsed JSON` callable, so tests can
    substitute recorded responses for the network.
    """
    name = ""
    licensed = False      # True only for sources whose terms allow commercial use
    sports = ("nba",)
    # When set (a timedelta), fetch only games starting within this window: the near-start
    # collector uses it to refresh closing lines every 15 minutes without re-fetching the slate.
    starting_within = None

    def __init__(self, http=None):
        self.http = http or default_http
        self.raw = {}     # last raw responses, for debugging parser breakage
        self.errors = []  # partial failures from the last fetch (the rest still returned)

    def available(self):
        """False when the source can't run (e.g. no API key configured)."""
        return True

    def fetch(self, sport="nba", days=2):
        raise NotImplementedError

    def _starts_soon(self, start, now=None):
        """True when no window is set, or `start` (UTC ISO 8601) is within it."""
        if self.starting_within is None:
            return True
        if not start:
            return False
        now = now or dt.datetime.now(dt.timezone.utc)
        return dt.datetime.fromisoformat(start.replace("Z", "+00:00")) <= now + self.starting_within

    def _check_sport(self, sport):
        if sport not in self.sports:
            raise ValueError(f"{self.name} does not support sport '{sport}'")


_REGISTRY = {}


def register(cls):
    if not cls.name:
        raise ValueError("odds source needs a name")
    _REGISTRY[cls.name] = cls
    return cls


def registered():
    return dict(_REGISTRY)


def licensed_sources():
    """Names of sources whose data may be used commercially."""
    return {name for name, cls in _REGISTRY.items() if cls.licensed}


def enabled_sources(http=None):
    """Sources named in ODDS_SOURCES (comma-separated), or every registered one, that are available."""
    names = [n.strip() for n in os.environ.get("ODDS_SOURCES", "").split(",") if n.strip()] or list(_REGISTRY)
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        raise ValueError(f"unknown odds source(s) {unknown}; known: {sorted(_REGISTRY)}")
    return [s for s in (_REGISTRY[n](http) for n in names) if s.available()]
