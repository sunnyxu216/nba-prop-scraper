"""Action Network public API adapter. DEVELOPMENT ONLY.

Unofficial and unlicensed: the feed can change without notice and its terms
don't allow commercial use, so `licensed = False`. Every offer carries
source="action_network" so this data can be excluded from anything commercial.

Props must be requested one market at a time (customPickType) per game; the
state code decides which books' lines are returned.
"""

import re
import time

from .base import OddsSource, Offer, register, utc_now

BASE = "https://api.actionnetwork.com/web"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
           "Accept": "application/json"}
MARKETS = {
    "core_bet_type_27_points": "PTS", "core_bet_type_23_rebounds": "REB", "core_bet_type_26_assists": "AST",
    "core_bet_type_21_3fgm": "3PM", "core_bet_type_24_steals": "STL", "core_bet_type_25_blocks": "BLK",
    "core_bet_type_580_turnovers": "TOV", "core_bet_type_85_points_rebounds_assists": "PRA",
    "core_bet_type_86_points_rebounds": "PR", "core_bet_type_87_points_assists": "PA",
    "core_bet_type_88_rebounds_assists": "RA", "core_bet_type_89_steals_blocks": "SB",
}
NOT_BOOKS = {"consensus", "open", "polymarket"}  # reference lines / exchange prices, not sportsbook odds
BOOK_ALIASES = {"DK": "DraftKings", "MGM": "BetMGM", "ESPNBet": "ESPN BET"}


def book_name(raw):
    """'DK NJ' -> 'DraftKings', 'FanDuel NJ' -> 'FanDuel'."""
    name = re.sub(r"\s+[A-Z]{2}$", "", raw.strip())
    return BOOK_ALIASES.get(name, name)


def parse_props(payload, books, game, observed_at):
    """One /v2/games/{id}/props response -> Offers.

    books: {book_id: display name}; game: {"id", "start", "home", "away", "teams": {team_id: code}}.
    """
    players = payload.get("players") or []
    if isinstance(players, dict):
        players = list(players.values())
    names = {p.get("id"): p.get("full_name") or "" for p in players}
    prices = {}
    for market, entries in (payload.get("player_props") or {}).items():
        stat = MARKETS.get(market)
        if not stat:
            continue
        for entry in entries:
            team = (game.get("teams") or {}).get(entry.get("team_id"), "")
            for book_id, outcomes in (entry.get("lines") or {}).items():
                book = book_name(books.get(int(book_id), str(book_id)))
                if book.lower() in NOT_BOOKS:
                    continue
                for o in outcomes:
                    if o.get("is_alt_market") or o.get("is_live") or o.get("side") not in ("over", "under"):
                        continue
                    pid = o.get("player_id") or entry.get("player_id")
                    if not names.get(pid) or o.get("odds") is None or o.get("value") is None:
                        continue
                    key = (pid, stat, book, float(o["value"]))
                    prices.setdefault(key, {"team": team})[o["side"]] = int(o["odds"])
    return [
        Offer(source=ActionNetwork.name, sport="nba", event_id=str(game["id"]), start_time=game.get("start") or "",
              home_team=game.get("home") or "", away_team=game.get("away") or "", player_name=names[pid],
              source_player_id=str(pid), team=px["team"], market=stat, book=book, line=line,
              over_odds=px["over"], under_odds=px["under"], observed_at=observed_at)
        for (pid, stat, book, line), px in prices.items() if "over" in px and "under" in px
    ]


@register
class ActionNetwork(OddsSource):
    name = "action_network"
    licensed = False

    def __init__(self, http=None, state="NJ", delay=0.25):
        super().__init__(http)
        self.state, self.delay = state, delay

    def _get(self, path, params=None):
        return self.http(BASE + path, params, HEADERS)

    def books(self):
        return {b["id"]: b["display_name"] for b in self._get("/v1/books").get("books", [])}

    def games(self, day):
        d = self._get("/v1/scoreboard/nba", {"date": day.strftime("%Y%m%d")})
        out = []
        for g in d.get("games", []):
            teams = {t["id"]: t.get("abbr") for t in g.get("teams", [])}
            out.append({"id": g["id"], "start": g.get("start_time"), "type": g.get("type"), "teams": teams,
                        "home": teams.get(g.get("home_team_id")), "away": teams.get(g.get("away_team_id"))})
        return out

    def fetch(self, sport="nba", days=2):
        import datetime as dt
        self._check_sport(sport)
        observed = utc_now()
        books = self.books()
        self.raw, self.errors, offers = {}, [], []
        for i in range(days):
            for game in self.games(dt.date.today() + dt.timedelta(days=i)):
                if game["type"] != "reg":
                    continue
                for market in MARKETS:
                    try:
                        payload = self._get(f"/v2/games/{game['id']}/props",
                                            {"customPickType": market, "stateCode": self.state})
                    except Exception as e:  # one failed market shouldn't lose the whole snapshot
                        self.errors.append(f"{game['away']}@{game['home']} {market}: {e}")
                        continue
                    finally:
                        if self.delay:
                            time.sleep(self.delay)
                    if payload.get("player_props"):
                        self.raw.setdefault(str(game["id"]), {})[market] = payload
                        offers += parse_props(payload, books, game, observed)
        return offers
