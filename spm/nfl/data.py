"""NFL history from nflverse (https://github.com/nflverse/nflverse-data), cached in data/cache/nfl/.

Datasets (CSV, gzipped, one file per season unless noted):
* stats_player_week: every player's weekly stat line (from play-by-play), 1999+
* snap_counts: offensive snap share per player-game (from Pro Football Reference), 2013+
* injuries: weekly injury report statuses, 2009+
* schedules/games.csv: every game incl. upcoming ones, with closing spread and total,
  roof, weather and rest days

Licensing: the nflverse-data repository is CC-BY-4.0 (commercial use with attribution).
Snap counts are scraped from Pro Football Reference, whose terms are stricter. PROVENANCE
below is stored with every trained model so commercial use can be checked dataset by dataset.

Past seasons are downloaded once; the current season is refreshed when older than 6 hours.
"""

import csv
import datetime as dt
import gzip
import os
import time
import urllib.error
import urllib.request

from ..data import CACHE_DIR, norm_name

RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
NFL_CACHE = os.path.join(CACHE_DIR, "nfl")
FIRST_SEASON = 2013           # first season with snap counts (nflverse's 2012 snap file is empty)
REFRESH_HOURS = 6
GZIP_MAGIC = bytes([0x1F, 0x8B])
PROVENANCE = {
    "stats_player_week": "nflverse (CC-BY-4.0), derived from NFL play-by-play",
    "snap_counts": "nflverse, scraped from Pro Football Reference (check PFR terms before commercial use)",
    "injuries": "nflverse (CC-BY-4.0), from NFL injury reports",
    "games": "nflverse (CC-BY-4.0); betting lines in it are closing lines from public sources",
}
# relocated franchises -> current codes, so team history is continuous
OLD_CODES = {"OAK": "LV", "SD": "LAC", "STL": "LA"}
STAT_MAP = {   # our field -> nflverse column
    "pass_att": "attempts", "pass_cmp": "completions", "pass_yds": "passing_yards", "pass_td": "passing_tds",
    "pass_int": "passing_interceptions", "rush_att": "carries", "rush_yds": "rushing_yards",
    "rush_td": "rushing_tds", "rec": "receptions", "rec_yds": "receiving_yards", "rec_td": "receiving_tds",
    "targets": "targets",
}
FIELDS = (*STAT_MAP, "other_td")
OTHER_TDS = ("special_teams_tds", "def_tds", "fumble_recovery_tds")   # count for Anytime TD


def current_season(today=None):
    today = today or dt.date.today()
    return today.year if today.month >= 8 else today.year - 1


def team(code):
    return OLD_CODES.get(code, code)


def _num(v):
    try:
        return float(v) if v not in ("", "NA", None) else 0.0
    except ValueError:
        return 0.0


def _opt(v):
    """Number, or None when nflverse has no value (dome weather, future games)."""
    return None if v in ("", "NA", None) else _num(v)


def _path(release, name):
    return os.path.join(NFL_CACHE, release, name)


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nfl-prop-model/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def fetch(release, name, refresh=False, log=print):
    """Download one release asset into the cache (if missing or stale); returns the local path.

    Saved under `name` either way; a few older files exist only uncompressed (name minus .gz),
    and _rows() reads both.
    """
    path = _path(release, name)
    fresh = os.path.exists(path) and (not refresh or time.time() - os.path.getmtime(path) < REFRESH_HOURS * 3600)
    if fresh:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = f"{RELEASES}/{release}/{name}"
    log(f"Downloading {release}/{name}")
    try:
        data = _download(url)
    except urllib.error.HTTPError as e:
        if e.code != 404 or not name.endswith(".gz"):
            raise
        data = _download(url[:-3])
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def _rows(path):
    with open(path, "rb") as f:
        gz = f.read(2) == GZIP_MAGIC
    opener = gzip.open if gz else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def download_all(seasons=None, log=print):
    """Fetch everything training and live predictions need."""
    cur = current_season()
    seasons = seasons or range(FIRST_SEASON, cur + 1)
    fetch("schedules", "games.csv.gz", refresh=True, log=log)
    for y in seasons:
        live = y == cur
        fetch("stats_player", f"stats_player_week_{y}.csv.gz", refresh=live, log=log)
        fetch("snap_counts", f"snap_counts_{y}.csv.gz", refresh=live, log=log)
        fetch("injuries", f"injuries_{y}.csv.gz", refresh=live, log=log)


def load_games():
    """game_id -> schedule row with typed fields (every season, incl. upcoming games)."""
    games = {}
    for r in _rows(_path("schedules", "games.csv.gz")):
        games[r["game_id"]] = {
            "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
            "game_type": r["game_type"], "gameday": r["gameday"], "gametime": r["gametime"],
            "home": team(r["home_team"]), "away": team(r["away_team"]),
            "home_score": None if r["home_score"] in ("", "NA") else int(r["home_score"]),
            "away_score": None if r["away_score"] in ("", "NA") else int(r["away_score"]),
            "spread": _num(r["spread_line"]),      # home team's expected margin (positive: home favored)
            "total": _num(r["total_line"]),
            "has_line": r["total_line"] not in ("", "NA"),
            "home_rest": _num(r["home_rest"]) or 7.0, "away_rest": _num(r["away_rest"]) or 7.0,
            "roof": r["roof"], "wind": _opt(r["wind"]), "temp": _opt(r["temp"]),
            "div_game": r["div_game"] == "1",
        }
    return games


def _snaps(seasons):
    """(game_id, team, player key) -> offensive snap share (0-1)."""
    out = {}
    for y in seasons:
        path = _path("snap_counts", f"snap_counts_{y}.csv.gz")
        if not os.path.exists(path):
            continue
        try:
            for r in _rows(path):
                out[(r["game_id"], team(r["team"]), norm_name(r["player"]))] = _num(r["offense_pct"])
        except (EOFError, OSError):   # a damaged upstream file costs that season's snap shares, not the run
            continue
    return out


def load_player_games(seasons=None):
    """Every player-game with stats, oldest first, joined to snap share and game context.

    Each row: ids, team/opponent, season/week, our stat FIELDS, target_share, air_yards_share,
    snap_pct, and the game's context from the team's side (home, spread, implied total, ...).
    """
    seasons = list(seasons or range(FIRST_SEASON, current_season() + 1))
    games, snaps = load_games(), _snaps(seasons)
    out = []
    for y in seasons:
        path = _path("stats_player", f"stats_player_week_{y}.csv.gz")
        if not os.path.exists(path):
            continue
        for r in _rows(path):
            g = games.get(r["game_id"])
            if g is None or r["season_type"] not in ("REG", "POST"):
                continue
            tm, key = team(r["team"]), norm_name(r["player_display_name"] or r["player_name"])
            home = tm == g["home"]
            row = {
                "player_id": r["player_id"], "name": r["player_display_name"] or r["player_name"],
                "key": key, "position": r["position"], "team": tm, "opp": g["away"] if home else g["home"],
                "season": int(r["season"]), "week": int(r["week"]), "game_id": r["game_id"],
                "gameday": g["gameday"], "post": r["season_type"] == "POST",
                **{f: _num(r[c]) for f, c in STAT_MAP.items()},
                "other_td": sum(_num(r.get(c)) for c in OTHER_TDS),
                "target_share": _num(r["target_share"]), "air_yards_share": _num(r["air_yards_share"]),
                "snap_pct": snaps.get((r["game_id"], tm, key)),
            }
            row.update(game_context(g, tm))
            out.append(row)
    out.sort(key=lambda r: (r["gameday"], r["game_id"], r["key"]))
    return out


def game_context(g, tm):
    """A game from one team's side: venue, market expectations, rest, weather."""
    home = tm == g["home"]
    spread = g["spread"] if home else -g["spread"]            # team's expected margin
    return {
        "home": 1.0 if home else 0.0,
        "spread": spread,
        "game_total": g["total"],
        "implied_pts": (g["total"] + spread) / 2 if g["has_line"] else None,
        "rest": g["home_rest"] if home else g["away_rest"],
        "opp_rest": g["away_rest"] if home else g["home_rest"],
        "dome": 1.0 if g["roof"] in ("dome", "closed") else 0.0,
        "wind": 0.0 if g["roof"] in ("dome", "closed") else g["wind"],
        "temp": g["temp"],
        "div_game": 1.0 if g["div_game"] else 0.0,
    }


def load_participation(seasons=None):
    """(game_id, team) -> keys of players who took an offensive snap (snap counts).

    Unlike stat rows, this includes players who played without recording a stat.
    """
    seasons = list(seasons or range(FIRST_SEASON, current_season() + 1))
    out = {}
    for (game_id, tm, key), pct in _snaps(seasons).items():
        if pct > 0:
            out.setdefault((game_id, tm), set()).add(key)
    return out


def load_injuries(season, week):
    """Latest injury-report status per (team, player key) for one week: 'Out', 'Doubtful', ..."""
    path = _path("injuries", f"injuries_{season}.csv.gz")
    out = {}
    if os.path.exists(path):
        for r in _rows(path):
            if int(r["week"]) == week and r["report_status"] not in ("", "NA"):
                out[(team(r["team"]), norm_name(r["full_name"]))] = r["report_status"]
    return out
