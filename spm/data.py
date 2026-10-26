"""Data layer.

Pulls league-wide player game logs and current rosters from stats.nba.com,
caches them under data/cache, and builds the in-memory Dataset the model reads.
"""

import bisect
import datetime as dt
import difflib
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict

# When packaged as an .exe, user data (props.csv, cache, config.json) lives next to the executable.
ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.environ.get("SPM_DATA_DIR") or os.path.join(ROOT, "data")   # override: cloud jobs, backfills
CACHE_DIR = os.path.join(DATA_DIR, "cache")

FIRST_SEASON = 2015              # first season of history (2015-16; play-type tracking starts here)
CURRENT_SEASON_TTL = 4 * 3600    # re-download in-progress season logs after 4h
ROSTER_TTL = 12 * 3600

STAT_FIELDS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Connection": "keep-alive",
}

TEAMS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


# --------------------------------------------------------------------------- helpers

def season_start(date_str=None):
    """Start year of the NBA season containing date_str (YYYY-MM-DD) or today."""
    d = dt.date.fromisoformat(date_str) if date_str else dt.date.today()
    return d.year if d.month >= 8 else d.year - 1


def season_label(start):
    return f"{start}-{str(start + 1)[-2:]}"


def norm_name(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['.\-]", "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(t for t in s.split() if t not in {"jr", "sr", "ii", "iii", "iv"})


def _http_json(url, params=None, timeout=60, retries=4, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            return json.loads(body)
        except Exception as e:  # network flakiness on stats.nba.com is common
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} tries: {last}")


def _read_cache(name):
    path = os.path.join(CACHE_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_cache(name, payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = os.path.join(CACHE_DIR, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(CACHE_DIR, name))


def _parse_min(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if ":" in str(v):
        m, s = str(v).split(":")
        return int(m) + int(s) / 60
    return float(v)


# --------------------------------------------------------------------------- downloads

def fetch_season_logs(start):
    """Every player box score for one regular season, as compact rows."""
    d = _http_json("https://stats.nba.com/stats/leaguegamelog", {
        "Counter": 0, "DateFrom": "", "DateTo": "", "Direction": "ASC", "LeagueID": "00",
        "PlayerOrTeam": "P", "Season": season_label(start),
        "SeasonType": "Regular Season", "Sorter": "DATE",
    })
    rs = d["resultSets"][0]
    ix = {h: i for i, h in enumerate(rs["headers"])}
    rows = []
    for r in rs["rowSet"]:
        matchup = r[ix["MATCHUP"]]
        home = " vs. " in matchup
        opp = matchup.split(" vs. " if home else " @ ")[-1].strip()
        rows.append([
            r[ix["PLAYER_ID"]], r[ix["PLAYER_NAME"]], r[ix["TEAM_ABBREVIATION"]],
            r[ix["GAME_ID"]], r[ix["GAME_DATE"]][:10], opp, 1 if home else 0,
            round(_parse_min(r[ix["MIN"]]), 2),
            *[r[ix[f.upper()]] or 0 for f in STAT_FIELDS],
        ])
    return rows


def fetch_roster(start):
    d = _http_json("https://stats.nba.com/stats/commonallplayers", {
        "IsOnlyCurrentSeason": 1, "LeagueID": "00", "Season": season_label(start),
    })
    rs = d["resultSets"][0]
    ix = {h: i for i, h in enumerate(rs["headers"])}
    out = []
    for r in rs["rowSet"]:
        team = r[ix["TEAM_ABBREVIATION"]]
        if not team:
            continue
        try:
            from_year = int(r[ix["FROM_YEAR"]])
        except (TypeError, ValueError):
            from_year = None
        out.append({"id": r[ix["PERSON_ID"]], "name": r[ix["DISPLAY_FIRST_LAST"]],
                    "team": team, "from_year": from_year})
    return out


def load_season_logs(start, current_start, force=False, log=print):
    name = f"logs_{start}.json"
    cached = _read_cache(name)
    completed = start < current_start
    fresh = cached and (completed or time.time() - cached["fetched"] < CURRENT_SEASON_TTL)
    if cached and fresh and not force:
        return cached["rows"], cached["fetched"]
    log(f"Downloading {season_label(start)} game logs from stats.nba.com ...")
    try:
        rows = fetch_season_logs(start)
    except Exception as e:
        if cached:
            log(f"  download failed ({e}); using cached copy")
            return cached["rows"], cached["fetched"]
        if not completed:
            log(f"  {season_label(start)} not available yet ({e})")
            return [], None
        raise
    now = time.time()
    _write_cache(name, {"fetched": now, "rows": rows})
    log(f"  {len(rows):,} player-games")
    return rows, now


def load_debut_years(log=print):
    """FROM_YEAR (rookie season start) for every player in league history."""
    cached = _read_cache("debut_years.json")
    if cached and time.time() - cached["fetched"] < 30 * 86400:
        return {int(k): v for k, v in cached["years"].items()}
    try:
        d = _http_json("https://stats.nba.com/stats/commonallplayers",
                       {"IsOnlyCurrentSeason": 0, "LeagueID": "00", "Season": season_label(season_start())})
        rs = d["resultSets"][0]
        ix = {h: i for i, h in enumerate(rs["headers"])}
        years = {}
        for r in rs["rowSet"]:
            try:
                years[r[ix["PERSON_ID"]]] = int(r[ix["FROM_YEAR"]])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log(f"  debut years unavailable ({e})")
        return {int(k): v for k, v in cached["years"].items()} if cached else {}
    _write_cache("debut_years.json", {"fetched": time.time(), "years": years})
    return years


def load_roster(current_start, force=False, log=print):
    name = f"roster_{current_start}.json"
    cached = _read_cache(name)
    if cached and not force and time.time() - cached["fetched"] < ROSTER_TTL:
        return cached["players"], cached["fetched"]
    log(f"Downloading {season_label(current_start)} rosters ...")
    try:
        players = fetch_roster(current_start)
    except Exception as e:
        if cached:
            log(f"  roster download failed ({e}); using cached copy")
            return cached["players"], cached["fetched"]
        log(f"  roster unavailable ({e}); falling back to last team played for")
        return None, None
    now = time.time()
    _write_cache(name, {"fetched": now, "players": players})
    log(f"  {len(players)} rostered players")
    return players, now


# --------------------------------------------------------------------------- dataset

class Game:
    __slots__ = ("game_id", "date", "season", "team", "opp", "home", "min") + STAT_FIELDS


class Player:
    __slots__ = ("id", "name", "team", "last_team", "from_year", "games", "dates", "on_roster")

    def __init__(self, pid, name):
        self.id, self.name = pid, name
        self.team = self.last_team = None
        self.from_year = None
        self.games, self.dates = [], []
        self.on_roster = False

    def games_before(self, asof=None):
        if asof is None:
            return self.games
        return self.games[:bisect.bisect_left(self.dates, asof)]

    @property
    def changed_team(self):
        return bool(self.team and self.last_team and self.team != self.last_team)


class Dataset:
    def __init__(self, season_rows, roster, current_start, meta, debut_years=None):
        self.current_start = current_start
        self.meta = meta
        self.calibration = None
        self.players = {}
        team_tot = defaultdict(lambda: dict.fromkeys(STAT_FIELDS, 0.0))
        team_opp = {}
        rate_num = defaultdict(lambda: dict.fromkeys(STAT_FIELDS, 0.0))
        rate_min = defaultdict(float)

        for start, rows in season_rows.items():
            for r in rows:
                pid, name, team, gid, date, opp, home, mn, *stats = r
                g = Game()
                g.game_id, g.date, g.season, g.team, g.opp, g.home, g.min = gid, date, start, team, opp, bool(home), mn
                for f, v in zip(STAT_FIELDS, stats):
                    setattr(g, f, v)
                p = self.players.get(pid)
                if p is None:
                    p = self.players[pid] = Player(pid, name)
                p.name = name
                p.games.append(g)
                tot = team_tot[(start, gid, team)]
                for f, v in zip(STAT_FIELDS, stats):
                    tot[f] += v
                team_opp[(start, gid, team)] = opp
                if mn >= 10:
                    rate_min[start] += mn
                    for f, v in zip(STAT_FIELDS, stats):
                        rate_num[start][f] += v

        for pid, p in self.players.items():
            p.from_year = (debut_years or {}).get(pid)
            p.games.sort(key=lambda g: g.date)
            p.dates = [g.date for g in p.games]
            if p.games:
                p.last_team = p.team = p.games[-1].team

        # What each defense allowed per game, and the league per-team-game average.
        allowed = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(STAT_FIELDS + ("n",), 0.0)))
        league = defaultdict(lambda: dict.fromkeys(STAT_FIELDS + ("n",), 0.0))
        for key, tot in team_tot.items():
            start = key[0]
            a = allowed[start][team_opp[key]]
            for f in STAT_FIELDS:
                a[f] += tot[f]
                league[start][f] += tot[f]
            a["n"] += 1
            league[start]["n"] += 1
        self.allowed = {s: {t: {f: (a[f] / a["n"] if f != "n" else a["n"]) for f in a} for t, a in teams.items()}
                        for s, teams in allowed.items()}
        self.league_avg = {s: {f: lg[f] / lg["n"] for f in STAT_FIELDS} for s, lg in league.items() if lg["n"]}
        self.league_rate = {s: {f: rate_num[s][f] / rate_min[s] for f in STAT_FIELDS}
                            for s in rate_min if rate_min[s]}

        if roster:
            for rp in roster:
                p = self.players.get(rp["id"])
                if p is None:
                    p = self.players[rp["id"]] = Player(rp["id"], rp["name"])
                p.team, p.on_roster = rp["team"], True
                p.from_year = rp["from_year"] or p.from_year
        else:
            latest = max((s for s, rows in season_rows.items() if rows), default=None)
            for p in self.players.values():
                p.on_roster = bool(p.games and p.games[-1].season == latest)

        self._name_index = {}
        for p in self.players.values():
            key = norm_name(p.name)
            # prefer rostered / most recent player on name collisions
            if key not in self._name_index or p.on_roster:
                self._name_index[key] = p.id
        self._opp_cache = {}

    # ----------------------------------------------------------------- lookups

    def find_player(self, name):
        key = norm_name(name)
        if key in self._name_index:
            return self.players[self._name_index[key]]
        close = difflib.get_close_matches(key, self._name_index.keys(), n=1, cutoff=0.88)
        return self.players[self._name_index[close[0]]] if close else None

    def latest_season_with(self, data, at_or_before):
        options = [s for s in data if s <= at_or_before]
        return max(options) if options else None

    def league_rate_for(self, fields, target):
        s = self.latest_season_with(self.league_rate, target - 1) or self.latest_season_with(self.league_rate, target)
        if s is None:
            return 0.0
        return sum(self.league_rate[s][f] for f in fields)

    def opp_factor(self, team, fields, target, include_current=True):
        """How much more/less of a stat this defense allows vs league average.

        Blends the current season (as games accumulate) with the previous one.
        """
        key = (team, tuple(fields), target, include_current)
        if key in self._opp_cache:
            return self._opp_cache[key]

        def ratios(start):
            teams = self.allowed.get(start)
            lg = self.league_avg.get(start)
            if not teams or not lg:
                return {}
            den = sum(lg[f] for f in fields)
            return {t: (sum(a[f] for f in fields) / den, a["n"], sum(a[f] for f in fields)) for t, a in teams.items()}

        prev = ratios(target - 1)
        cur = ratios(target) if include_current else {}
        blended = {}
        for t in set(prev) | set(cur):
            p, c = prev.get(t), cur.get(t)
            if c and p:
                w = c[1] / (c[1] + 15)
                blended[t] = (w * c[0] + (1 - w) * p[0], w * c[2] + (1 - w) * p[2], c[1])
            elif c or p:
                x = c or p
                blended[t] = (x[0], x[2], c[1] if c else 0)
        result = None
        if team in blended:
            order = sorted(blended, key=lambda t: -blended[t][0])
            ratio, per_game, n_cur = blended[team]
            lg_season = target if cur and team in cur and not prev else target - 1
            lg = self.league_avg.get(lg_season) or self.league_avg.get(target)
            result = {
                "ratio": ratio, "allowed": per_game,
                "league": sum(lg[f] for f in fields) if lg else None,
                "rank": order.index(team) + 1, "teams": len(order),
                "basis": season_label(target - 1) if not n_cur else
                f"{season_label(target)} ({n_cur} gms) blended with {season_label(target - 1)}",
            }
        self._opp_cache[key] = result
        return result

    def teams(self):
        return sorted({p.team for p in self.players.values() if p.on_roster and p.team})


def load_dataset(force=False, log=print):
    current = season_start()
    seasons = list(range(FIRST_SEASON, current + 1))
    season_rows, fetched = {}, {}
    for s in seasons:
        rows, ts = load_season_logs(s, current, force=force, log=log)
        season_rows[s], fetched[s] = rows, ts
    roster, roster_ts = load_roster(current, force=force, log=log)
    debut_years = load_debut_years(log=log)
    meta = {
        "current_season": season_label(current),
        "seasons": [{"season": season_label(s), "rows": len(season_rows[s]), "fetched": fetched[s]} for s in seasons],
        "roster_players": len(roster) if roster else 0,
        "roster_fetched": roster_ts,
        "built": time.time(),
    }
    log("Building dataset ...")
    return Dataset(season_rows, roster, current, meta, debut_years)
