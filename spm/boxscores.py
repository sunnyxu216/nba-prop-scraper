"""Box scores for grading: every player's stat line for a range of dates.

Two sources, chosen with BOXSCORE_SOURCE:
* "espn" (default): ESPN's public scoreboard + game summaries. Works from cloud
  servers such as GitHub Actions.
* "nba": stats.nba.com's date-filtered league game log (one request), the same
  source the model trains on. It silently drops requests from cloud IPs, so
  it's only usable from a home connection.

Standalone (stdlib only) so it can run in GitHub Actions.
"""

import datetime as dt
import os

from .data import HEADERS, _http_json, norm_name, season_label, season_start


def _min(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if ":" in str(v):
        m, s = str(v).split(":")
        return int(m) + int(s) / 60
    return float(v)


ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_TEAMS = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}


def fetch(date_from, date_to, http=None, source=None):
    """Results rows for games played from date_from to date_to (inclusive, US dates)."""
    source = source or os.environ.get("BOXSCORE_SOURCE", "espn")
    if source == "espn":
        return fetch_espn(date_from, date_to, http)
    if source == "nba":
        return fetch_nba(date_from, date_to, http)
    raise ValueError(f"unknown BOXSCORE_SOURCE {source!r} (use 'espn' or 'nba')")


def fetch_espn(date_from, date_to, http=None):
    """One scoreboard request per day, then one summary per finished game."""
    http = http or (lambda url, params: _http_json(url, params, headers={"User-Agent": "Mozilla/5.0"},
                                                   retries=3, timeout=30))
    code = lambda abbr: ESPN_TEAMS.get(abbr, abbr)
    rows = []
    day = date_from
    while day <= date_to:
        board = http(f"{ESPN}/scoreboard", {"dates": day.strftime("%Y%m%d")})
        for ev in board.get("events", []):
            comp = ev["competitions"][0]
            if not comp["status"]["type"].get("completed"):
                continue
            summary = http(f"{ESPN}/summary", {"event": ev["id"]})
            teams = [code(t["team"]["abbreviation"]) for t in comp["competitors"]]
            for side in summary.get("boxscore", {}).get("players", []):
                team = code(side["team"]["abbreviation"])
                opp = next((t for t in teams if t != team), "")
                block = side["statistics"][0]
                col = {name: i for i, name in enumerate(block.get("labels") or block.get("names"))}
                for a in block.get("athletes", []):
                    st = a.get("stats") or []
                    if a.get("didNotPlay") or not st:
                        continue
                    num = lambda label: int(str(st[col[label]]).split("-")[0] or 0)
                    rows.append({
                        "sport": "nba", "game_date": day, "source_game_id": f"espn:{ev['id']}",
                        "player_key": norm_name(a["athlete"]["displayName"]),
                        "player_name": a["athlete"]["displayName"], "team": team, "opponent": opp,
                        "minutes": float(num("MIN")), "pts": num("PTS"), "reb": num("REB"), "ast": num("AST"),
                        "fg3m": num("3PT"), "stl": num("STL"), "blk": num("BLK"), "tov": num("TO"),
                    })
        day += dt.timedelta(days=1)
    return rows


def fetch_nba(date_from, date_to, http=None):
    """stats.nba.com league game log for a date range (home connections only)."""
    http = http or (lambda url, params: _http_json(url, params, headers=HEADERS, retries=3, timeout=60))
    d = http("https://stats.nba.com/stats/leaguegamelog", {
        "Counter": 0, "Direction": "ASC", "LeagueID": "00", "PlayerOrTeam": "P",
        "Season": season_label(season_start(date_from.isoformat())), "SeasonType": "Regular Season",
        "Sorter": "DATE", "DateFrom": date_from.strftime("%m/%d/%Y"), "DateTo": date_to.strftime("%m/%d/%Y"),
    })
    rs = d["resultSets"][0]
    ix = {h: i for i, h in enumerate(rs["headers"])}
    rows = []
    for r in rs["rowSet"]:
        matchup = r[ix["MATCHUP"]]
        sep = " vs. " if " vs. " in matchup else " @ "
        rows.append({
            "sport": "nba", "game_date": dt.date.fromisoformat(r[ix["GAME_DATE"]][:10]),
            "source_game_id": r[ix["GAME_ID"]], "player_key": norm_name(r[ix["PLAYER_NAME"]]),
            "player_name": r[ix["PLAYER_NAME"]], "team": r[ix["TEAM_ABBREVIATION"]],
            "opponent": matchup.split(sep)[-1].strip(), "minutes": round(_min(r[ix["MIN"]]), 1),
            **{f: int(r[ix[f.upper()]] or 0) for f in ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")},
        })
    return rows
