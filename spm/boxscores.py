"""Box scores for grading: every player's stat line for a range of dates.

Uses stats.nba.com's league game log filtered by date, which is one small
request per run. Standalone (stdlib only) so it can run in GitHub Actions.
"""

import datetime as dt

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


def fetch(date_from, date_to, http=None):
    """Results rows for games played from date_from to date_to (inclusive, US dates)."""
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
