"""NFL box scores for grading, from ESPN's public API (works from GitHub Actions).

Per finished game:
* summary: stats by category (passing, rushing, receiving, returns, defense). A player
  appears only in categories where he recorded something;
* each team's game roster (core API): who actually played (`didNotPlay`), by ESPN id,
  so a player who played without a stat grades as 0 rather than void;
* each team's current roster: ESPN id -> full name (game rosters only carry last names).

Standalone (stdlib only) so it can run in GitHub Actions.
"""

import datetime as dt

from ..data import _http_json, norm_name
from . import teams

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
STAT_FIELDS = ("pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "rush_att", "rush_yds", "rush_td",
               "rec", "rec_yds", "rec_td", "targets", "other_td")
# (ESPN category, label) -> our field. Passing "C/ATT" ("22/31") is split separately.
COLUMNS = {
    ("passing", "YDS"): "pass_yds", ("passing", "TD"): "pass_td", ("passing", "INT"): "pass_int",
    ("rushing", "CAR"): "rush_att", ("rushing", "YDS"): "rush_yds", ("rushing", "TD"): "rush_td",
    ("receiving", "REC"): "rec", ("receiving", "YDS"): "rec_yds", ("receiving", "TD"): "rec_td",
    ("receiving", "TGTS"): "targets",
}
# Non-offensive TDs. "defensive" TD can include interception-return TDs, so take the larger of the
# two rather than adding them (only "scored at least one" matters for Anytime TD).
RETURN_TDS = ("kickReturns", "puntReturns")


def _int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return 0


def parse_game(summary, game_rosters, names, day):
    """One finished game -> results rows.

    game_rosters: {team code: core-API game roster json}; names: {espn athlete id (str): full name}.
    """
    event_id = summary["header"]["id"]
    sides = summary.get("boxscore", {}).get("players", [])
    codes = [teams.code(s["team"]["abbreviation"]) for s in sides]
    stats, team_of, box_names = {}, {}, {}
    for side, team in zip(sides, codes):
        for cat in side["statistics"]:
            labels = cat.get("labels") or []
            for a in cat.get("athletes", []):
                pid = str(a["athlete"]["id"])
                st = stats.setdefault(pid, dict.fromkeys(STAT_FIELDS, 0))
                team_of[pid], box_names[pid] = team, a["athlete"]["displayName"]
                vals = dict(zip(labels, a.get("stats") or []))
                if cat["name"] == "passing" and "/" in str(vals.get("C/ATT", "")):
                    cmp_, att = str(vals["C/ATT"]).split("/")
                    st["pass_cmp"], st["pass_att"] = _int(cmp_), _int(att)
                for (c, label), field in COLUMNS.items():
                    if c == cat["name"] and label in vals:
                        st[field] = _int(vals[label])
                if cat["name"] in RETURN_TDS:
                    st["other_td"] += _int(vals.get("TD", 0))
                elif cat["name"] in ("defensive", "interceptions"):
                    st["_def_td"] = max(st.get("_def_td", 0), _int(vals.get("TD", 0)))
    played = {}
    for team, roster in game_rosters.items():
        for e in roster.get("entries", []):
            if not e.get("didNotPlay"):
                played[str(e["playerId"])] = team
    rows = []
    for pid in set(played) | set(stats):      # box-score stats prove a player played, even if the roster lags
        name = names.get(pid) or box_names.get(pid)
        team = played.get(pid) or team_of.get(pid)
        if not name or not team:
            continue
        st = dict(stats.get(pid) or dict.fromkeys(STAT_FIELDS, 0))
        st["other_td"] += st.pop("_def_td", 0)
        rows.append({"sport": "nfl", "game_date": day, "source_game_id": f"espn:{event_id}",
                     "player_key": norm_name(name), "player_name": name, "source_player_id": pid, "team": team,
                     "opponent": next((t for t in codes if t != team), ""), **st})
    return rows


def fetch(date_from, date_to, http=None):
    """Results rows for finished games from date_from to date_to (inclusive, US Eastern dates)."""
    http = http or (lambda url, params=None: _http_json(url, params, headers={"User-Agent": "Mozilla/5.0"},
                                                        retries=3, timeout=30))
    names = {}           # espn id -> full name, filled per team as needed
    loaded_teams = set()
    rows, day = [], date_from
    while day <= date_to:
        board = http(f"{SITE}/scoreboard", {"dates": day.strftime("%Y%m%d")})
        for ev in board.get("events", []):
            comp = ev["competitions"][0]
            if not comp["status"]["type"].get("completed"):
                continue
            # ESPN's NFL scoreboard can also list the week's other days; keep this day's games only
            kickoff = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            if (kickoff - dt.timedelta(hours=5)).date() != day:
                continue
            summary = http(f"{SITE}/summary", {"event": ev["id"]})
            game_rosters = {}
            for c in comp["competitors"]:
                tid, code = c["team"]["id"], teams.code(c["team"]["abbreviation"])
                game_rosters[code] = http(f"{CORE}/events/{ev['id']}/competitions/{ev['id']}/competitors/{tid}/roster")
                if tid not in loaded_teams:
                    roster = http(f"{SITE}/teams/{tid}/roster")
                    names.update({str(a["id"]): a["fullName"] for grp in roster.get("athletes", [])
                                  for a in grp.get("items", [])})
                    loaded_teams.add(tid)
            rows += parse_game(summary, game_rosters, names, day)
        day += dt.timedelta(days=1)
    return rows
