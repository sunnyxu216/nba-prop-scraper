"""Optional: pull real player-prop lines from The Odds API (https://the-odds-api.com).

Set an API key in config.json ("odds_api_key") or the ODDS_API_KEY env var.
Each event's props costs request credits, so imports are capped by max_events.
"""

import datetime as dt
import json
import os

from .data import ROOT, TEAMS, _http_json

CONFIG_PATH = os.path.join(ROOT, "config.json")
BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"

MARKETS = {
    "player_points": "PTS", "player_rebounds": "REB", "player_assists": "AST",
    "player_threes": "3PM", "player_points_rebounds_assists": "PRA",
    "player_blocks": "BLK", "player_steals": "STL", "player_turnovers": "TOV",
    "player_points_rebounds": "PR", "player_points_assists": "PA",
    "player_rebounds_assists": "RA", "player_blocks_steals": "SB",
}
NAME_TO_CODE = {v: k for k, v in TEAMS.items()}
NAME_TO_CODE["Los Angeles Clippers"] = "LAC"

DEFAULT_CONFIG = {
    "odds_api_key": "",
    # Empty = every book in the regions below. Or list keys, e.g. ["draftkings", "fanduel", "fanatics"].
    "bookmakers": [],
    # "us" covers the major US books; add "us2" for more (each region costs credits per market).
    "regions": ["us"],
    "max_events": 5,
    "markets": ["player_points", "player_rebounds", "player_assists", "player_threes",
                "player_points_rebounds_assists"],
}


def config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["odds_api_key"] = os.environ.get("ODDS_API_KEY") or cfg["odds_api_key"]
    return cfg


def enabled():
    return bool(config()["odds_api_key"])


def fetch_props(ds):
    cfg = config()
    key = cfg["odds_api_key"]
    if not key:
        raise RuntimeError("No Odds API key configured (config.json or ODDS_API_KEY)")
    hdr = {"User-Agent": "nba-prop-model/1.0"}
    events = _http_json(f"{BASE}/events", {"apiKey": key}, headers=hdr, retries=2)
    events.sort(key=lambda e: e["commence_time"])
    props, unmatched = [], set()
    # Every date each team plays: games already logged this season plus the upcoming schedule.
    team_dates = {}
    for p in ds.players.values():
        for g in p.games:
            if g.season == ds.current_start:
                team_dates.setdefault(g.team, set()).add(g.date)
    for ev in events:
        local = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone().date()
        for name in (ev["home_team"], ev["away_team"]):
            if NAME_TO_CODE.get(name):
                team_dates.setdefault(NAME_TO_CODE[name], set()).add(local.isoformat())

    def b2b(team, day):
        return (day - dt.timedelta(days=1)).isoformat() in team_dates.get(team, set())

    for ev in events[: int(cfg["max_events"])]:
        home, away = NAME_TO_CODE.get(ev["home_team"]), NAME_TO_CODE.get(ev["away_team"])
        start = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone()
        params = {"apiKey": key, "oddsFormat": "american", "markets": ",".join(cfg["markets"])}
        if cfg.get("bookmakers"):
            params["bookmakers"] = ",".join(cfg["bookmakers"])
        else:
            params["regions"] = ",".join(cfg.get("regions") or ["us"])
        odds = _http_json(f"{BASE}/events/{ev['id']}/odds", params, headers=hdr, retries=2)
        for bk in odds.get("bookmakers", []):
            for m in bk.get("markets", []):
                stat = MARKETS.get(m["key"])
                if not stat:
                    continue
                pairs = {}
                for o in m.get("outcomes", []):
                    k = (o.get("description"), o.get("point"))
                    pairs.setdefault(k, {})[o["name"].lower()] = o["price"]
                for (name, point), px in pairs.items():
                    if point is None or "over" not in px or "under" not in px:
                        continue
                    player = ds.find_player(name)
                    if not player:
                        unmatched.add(name)
                        continue
                    is_home = player.team == home if player.team in (home, away) else None
                    opp = (away if is_home else home) if is_home is not None else ""
                    props.append({
                        "game_date": start.date().isoformat(), "player": player.name, "stat": stat,
                        "line": point, "over_odds": px["over"], "under_odds": px["under"],
                        "opponent": opp or "", "home": is_home, "b2b": b2b(player.team, start.date()),
                        "book": bk.get("title") or bk.get("key", ""), "source": "odds_api",
                    })
    return props, sorted(unmatched)
