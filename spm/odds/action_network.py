"""Action Network public API adapter. DEVELOPMENT ONLY.

Unofficial and unlicensed: the feed can change without notice and its terms
don't allow commercial use, so `licensed = False`. Every offer carries
source="action_network" so this data can be excluded from anything commercial.

Props must be requested one market at a time (customPickType) per game; the
state code decides which books' lines are returned.

NBA games are listed per day. The NFL scoreboard ignores `date` and lists one
week at a time (`week=N`, default the current week), so NFL fetches the current
and next week and keeps the games kicking off within `days`.
"""

import datetime as dt
import re
import time

from ..nfl import teams as nfl_teams
from .base import ONE_SIDED, OddsSource, Offer, register, utc_now

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
NFL_MARKETS = {  # ids from the market list on actionnetwork.com/nfl/props (Oct 2026)
    "core_bet_type_9_passing_yards": "PASS_YDS", "core_bet_type_10_pass_completions": "PASS_CMP",
    "core_bet_type_30_passing_attempts": "PASS_ATT", "core_bet_type_11_passing_tds": "PASS_TD",
    "core_bet_type_65_interceptions": "INT", "core_bet_type_12_rushing_yards": "RUSH_YDS",
    "core_bet_type_18_rushing_attempts": "RUSH_ATT", "core_bet_type_15_receptions": "REC",
    "core_bet_type_16_receiving_yards": "REC_YDS", "core_bet_type_66_rushing_receiving_yards": "RUSH_REC_YDS",
    "core_bet_type_62_anytime_touchdown_scorer": "ANYTIME_TD",
}
SPORT_MARKETS = {"nba": MARKETS, "nfl": NFL_MARKETS}
GAME_TYPES = {"nba": {"reg"}, "nfl": {"reg", "post"}}
NOT_BOOKS = {"consensus", "open", "polymarket"}  # reference lines / exchange prices, not sportsbook odds
BOOK_ALIASES = {"DK": "DraftKings", "MGM": "BetMGM", "ESPNBet": "ESPN BET", "HardRock": "Hard Rock"}


def book_name(raw):
    """'DK NJ' -> 'DraftKings', 'FanDuel NJ' -> 'FanDuel', 'UnibetNJ' -> 'Unibet'."""
    name = re.sub(r"\s+[A-Z]{2}$", "", raw.strip())
    name = re.sub(r"(?<=[a-z])[A-Z]{2}$", "", name)   # state glued on: 'UnibetNJ' (but not 'BetMGM')
    return BOOK_ALIASES.get(name, name)


def parse_props(payload, books, game, observed_at, sport="nba"):
    """One /v2/games/{id}/props response -> Offers.

    books: {book_id: display name}; game: {"id", "start", "home", "away", "teams": {team_id: code}}.
    One-sided markets (Anytime TD) come as side "noside" with value 0: stored as over 0.5,
    with under_odds None unless the book also posts an under ("no") price.
    """
    markets = SPORT_MARKETS[sport]
    players = payload.get("players") or []
    if isinstance(players, dict):
        players = list(players.values())
    names = {p.get("id"): p.get("full_name") or "" for p in players}
    prices = {}
    for market, entries in (payload.get("player_props") or {}).items():
        stat = markets.get(market)
        if not stat:
            continue
        one_sided = stat in ONE_SIDED
        for entry in entries:
            team = (game.get("teams") or {}).get(entry.get("team_id"), "")
            for book_id, outcomes in (entry.get("lines") or {}).items():
                book = book_name(books.get(int(book_id), str(book_id)))
                if book.lower() in NOT_BOOKS:
                    continue
                for o in outcomes:
                    side = "over" if one_sided and o.get("side") == "noside" else o.get("side")
                    if o.get("is_alt_market") or o.get("is_live") or side not in ("over", "under"):
                        continue
                    pid = o.get("player_id") or entry.get("player_id")
                    if not names.get(pid) or o.get("odds") is None or o.get("value") is None:
                        continue
                    key = (pid, stat, book, 0.5 if one_sided else float(o["value"]))
                    prices.setdefault(key, {"team": team})[side] = int(o["odds"])
    return [
        Offer(source=ActionNetwork.name, sport=sport, event_id=str(game["id"]), start_time=game.get("start") or "",
              home_team=game.get("home") or "", away_team=game.get("away") or "", player_name=names[pid],
              source_player_id=str(pid), team=px["team"], market=stat, book=book, line=line,
              over_odds=px["over"], under_odds=px.get("under"), observed_at=observed_at)
        for (pid, stat, book, line), px in prices.items()
        if "over" in px and ("under" in px or stat in ONE_SIDED)
    ]


def parse_games(scoreboard, sport="nba"):
    """A /v1/scoreboard/{sport} response -> game dicts (NFL team codes made canonical)."""
    team_code = nfl_teams.code if sport == "nfl" else (lambda abbr: abbr)
    out = []
    for g in scoreboard.get("games", []):
        teams = {t["id"]: team_code(t.get("abbr")) for t in g.get("teams", [])}
        out.append({"id": g["id"], "start": g.get("start_time"), "type": g.get("type"), "week": g.get("week"),
                    "teams": teams, "home": teams.get(g.get("home_team_id")),
                    "away": teams.get(g.get("away_team_id"))})
    return out


@register
class ActionNetwork(OddsSource):
    name = "action_network"
    licensed = False
    sports = ("nba", "nfl")

    def __init__(self, http=None, state="NJ", delay=0.25, now=None):
        super().__init__(http)
        self.state, self.delay = state, delay
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))   # injectable clock (tests)

    def _get(self, path, params=None):
        return self.http(BASE + path, params, HEADERS)

    def books(self):
        return {b["id"]: b["display_name"] for b in self._get("/v1/books").get("books", [])}

    def games(self, day):
        return parse_games(self._get("/v1/scoreboard/nba", {"date": day.strftime("%Y%m%d")}))

    def nfl_games(self, days):
        """Games of the current and next NFL week that kick off in the next `days` days."""
        now = self.now()
        current = parse_games(self._get("/v1/scoreboard/nfl"), "nfl")
        week = max((g["week"] for g in current if g["week"]), default=None)
        nxt = parse_games(self._get("/v1/scoreboard/nfl", {"week": week + 1}), "nfl") if week else []
        out, seen = [], set()
        for g in current + nxt:
            if g["id"] in seen or not g["start"]:
                continue
            seen.add(g["id"])
            start = dt.datetime.fromisoformat(g["start"].replace("Z", "+00:00"))
            if now < start <= now + dt.timedelta(days=days):
                out.append(g)
        return out

    def fetch(self, sport="nba", days=2):
        self._check_sport(sport)
        observed = utc_now()
        books = self.books()
        self.raw, self.errors, offers = {}, [], []
        if sport == "nfl":
            games = self.nfl_games(days)
        else:
            games = [g for i in range(days) for g in self.games(dt.date.today() + dt.timedelta(days=i))]
        for game in games:
            if game["type"] not in GAME_TYPES[sport]:
                continue
            for market in SPORT_MARKETS[sport]:
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
                    offers += parse_props(payload, books, game, observed, sport)
        return offers
