"""The Odds API adapter (https://the-odds-api.com). LICENSED: the intended source at launch.

Needs an API key: ODDS_API_KEY env var, or "odds_api_key" in config.json.
Cost: 1 credit per market returned, per region (10 books), per event.
"""

import datetime as dt
import json
import os

from ..data import ROOT, TEAMS
from ..nfl import teams as nfl_teams
from .base import ONE_SIDED, OddsSource, Offer, register, utc_now

BASE = "https://api.the-odds-api.com/v4/sports"
SPORT_KEYS = {"nba": "basketball_nba", "nfl": "americanfootball_nfl"}
MARKETS = {
    "player_points": "PTS", "player_rebounds": "REB", "player_assists": "AST",
    "player_threes": "3PM", "player_points_rebounds_assists": "PRA",
    "player_blocks": "BLK", "player_steals": "STL", "player_turnovers": "TOV",
    "player_points_rebounds": "PR", "player_points_assists": "PA",
    "player_rebounds_assists": "RA", "player_blocks_steals": "SB",
}
NFL_MARKETS = {
    "player_pass_yds": "PASS_YDS", "player_pass_completions": "PASS_CMP", "player_pass_attempts": "PASS_ATT",
    "player_pass_tds": "PASS_TD", "player_pass_interceptions": "INT", "player_rush_yds": "RUSH_YDS",
    "player_rush_attempts": "RUSH_ATT", "player_receptions": "REC", "player_reception_yds": "REC_YDS",
    "player_rush_reception_yds": "RUSH_REC_YDS", "player_anytime_td": "ANYTIME_TD",
}
SPORT_MARKETS = {"nba": MARKETS, "nfl": NFL_MARKETS}
NAME_TO_CODE = {
    "nba": {v: k for k, v in TEAMS.items()} | {"Los Angeles Clippers": "LAC"},
    "nfl": nfl_teams.NAME_TO_CODE,
}
DEFAULT_CONFIG = {
    "odds_api_key": "",
    "bookmakers": [],     # empty = every book in `regions`
    "regions": ["us"],
    "max_events": 5,
    "markets": ["player_points", "player_rebounds", "player_assists", "player_threes",
                "player_points_rebounds_assists"],
    "nfl_markets": ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions",
                    "player_pass_tds", "player_anytime_td"],
}


def config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    cfg["odds_api_key"] = os.environ.get("ODDS_API_KEY") or cfg["odds_api_key"]
    return cfg


def parse_event_odds(odds, event, observed_at, sport="nba"):
    """One /events/{id}/odds response -> Offers.

    One-sided markets (Anytime TD) have Yes/No outcomes and no point: stored as over 0.5
    ("yes"), with under_odds None when the book posts no "no" price.
    """
    names, markets = NAME_TO_CODE[sport], SPORT_MARKETS[sport]
    home, away = names.get(event["home_team"], ""), names.get(event["away_team"], "")
    offers = []
    for bk in odds.get("bookmakers", []):
        book = bk.get("title") or bk.get("key", "")
        for m in bk.get("markets", []):
            stat = markets.get(m.get("key"))
            if not stat:
                continue
            one_sided = stat in ONE_SIDED
            pairs = {}
            for o in m.get("outcomes", []):
                side = {"yes": "over", "no": "under"}.get(o["name"].lower(), o["name"].lower())
                point = 0.5 if one_sided else o.get("point")
                pairs.setdefault((o.get("description"), point), {})[side] = o["price"]
            for (name, point), px in pairs.items():
                if not name or point is None or "over" not in px or ("under" not in px and not one_sided):
                    continue
                offers.append(Offer(
                    source=TheOddsAPI.name, sport=sport, event_id=event["id"], start_time=event["commence_time"],
                    home_team=home, away_team=away, player_name=name, source_player_id="", team="",
                    market=stat, book=book, line=float(point), over_odds=int(px["over"]),
                    under_odds=int(px["under"]) if "under" in px else None, observed_at=observed_at))
    return offers


@register
class TheOddsAPI(OddsSource):
    name = "the_odds_api"
    licensed = True
    sports = ("nba", "nfl")

    def __init__(self, http=None, cfg=None):
        super().__init__(http)
        self.cfg = cfg or config()

    def available(self):
        return bool(self.cfg["odds_api_key"])

    def fetch(self, sport="nba", days=2):
        self._check_sport(sport)
        key, cfg = self.cfg["odds_api_key"], self.cfg
        base = f"{BASE}/{SPORT_KEYS[sport]}"
        hdr = {"User-Agent": "nba-prop-model/1.0"}
        observed = utc_now()
        horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
        events = sorted(self.http(f"{base}/events", {"apiKey": key}, hdr), key=lambda e: e["commence_time"])
        events = [e for e in events
                  if dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) <= horizon]
        markets = cfg["nfl_markets"] if sport == "nfl" else cfg["markets"]
        self.raw, offers = {}, []
        for ev in events[: int(cfg["max_events"])]:
            params = {"apiKey": key, "oddsFormat": "american", "markets": ",".join(markets)}
            if cfg.get("bookmakers"):
                params["bookmakers"] = ",".join(cfg["bookmakers"])
            else:
                params["regions"] = ",".join(cfg.get("regions") or ["us"])
            odds = self.http(f"{base}/events/{ev['id']}/odds", params, hdr)
            self.raw[ev["id"]] = odds
            offers += parse_event_odds(odds, ev, observed, sport)
        return offers
