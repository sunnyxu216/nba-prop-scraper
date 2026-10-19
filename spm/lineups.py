"""Who's playing: teammate and opponent availability.

For every historical game we know exactly who played for both teams (box
scores). Each team's *rotation* going into a game is the players currently on
that team who played for it in its last 10 games (or, before a team has played
3 games in a season, everyone on its roster with NBA history). Rotation players
who didn't play are *missing*.

All player values (expected minutes, points, rebounds, assists, steals+blocks)
are pre-game expectations from the feature engine's running averages, never the
minutes or stats from the game itself, so nothing leaks from the result.

Live, "missing" comes from the injury report (Out / Doubtful).
"""

import datetime as dt
from bisect import bisect_left
from collections import defaultdict, deque

import numpy as np

from .data import _http_json, _read_cache, _write_cache, season_label

ROTATION_GAMES = 10       # look-back window of team games
ROTATION_APPEARANCES = 3  # must have played in this many of them to count as rotation
ROTATION_MIN = 12.0
SEASON_START_MIN = 15.0   # stricter bar before a team has played SEASON_START_GAMES games
SEASON_START_GAMES = 3

LINEUP_FEATS = [
    "tm_missing_min", "tm_missing_pts", "tm_missing_ast", "tm_missing_reb", "tm_top_out", "own_pts_share",
    "opp_missing_min", "opp_missing_def", "opp_missing_stocks", "opp_missing_reb", "opp_active_def",
]
LINEUP_LABELS = {
    "tm_missing_min": "Teammates out (minutes)", "tm_missing_pts": "Teammates out (scoring share)",
    "tm_missing_ast": "Teammates out (assist share)", "tm_missing_reb": "Teammates out (rebound share)",
    "tm_top_out": "Team's top scorer out", "own_pts_share": "Player's share of team scoring",
    "opp_missing_min": "Opponents out (minutes)", "opp_missing_def": "Opponent defense missing",
    "opp_missing_stocks": "Opponent steals+blocks missing", "opp_missing_reb": "Opponent rebounding missing",
    "opp_active_def": "Opponent defenders playing",
}
OUT_STATUSES = {"out", "doubtful", "suspension", "suspended", "inactive"}
# Chance a listed player sits, by injury-report status.
P_OUT = {"out": 1.0, "suspension": 1.0, "suspended": 1.0, "inactive": 1.0, "doubtful": 0.85,
         "questionable": 0.5, "day-to-day": 0.35, "game time decision": 0.5, "probable": 0.1}


# --------------------------------------------------------------------------- defensive value

def fetch_defense(start):
    """Defensive win shares per player for a season (stats.nba.com)."""
    base = {"LeagueID": "00", "Season": season_label(start), "SeasonType": "Regular Season", "PerMode": "Totals",
            "MeasureType": "Defense", "DateFrom": "", "DateTo": "", "GameScope": "", "GameSegment": "",
            "LastNGames": 0, "Location": "", "Month": 0, "OpponentTeamID": 0, "Outcome": "", "PORound": 0,
            "PaceAdjust": "N", "Period": 0, "PlayerExperience": "", "PlayerPosition": "", "PlusMinus": "N",
            "Rank": "N", "SeasonSegment": "", "StarterBench": "", "TeamID": 0, "VsConference": "",
            "VsDivision": "", "Conference": "", "Division": "", "College": "", "Country": "", "DraftPick": "",
            "DraftYear": "", "Height": "", "Weight": "", "ShotClockRange": "", "TwoWay": 0}
    rs = _http_json("https://stats.nba.com/stats/leaguedashplayerstats", base)["resultSets"][0]
    ix = {h: i for i, h in enumerate(rs["headers"])}
    return {str(r[ix["PLAYER_ID"]]): [float(r[ix["MIN"]] or 0), float(r[ix["DEF_WS"]] or 0)] for r in rs["rowSet"]}


def load_defense(first, current_start, log=print):
    """{season: {pid: defensive win shares per 36 minutes}}, shrunk toward league average."""
    out = {}
    for s in range(first, current_start):
        name = f"defense_{s}.json"
        cached = _read_cache(name)
        if cached is None:
            log(f"Downloading {season_label(s)} defensive ratings ...")
            try:
                cached = {"players": fetch_defense(s)}
                _write_cache(name, cached)
            except Exception as e:
                log(f"  unavailable ({e})")
                continue
        rows = cached["players"]
        tot_min = sum(m for m, _ in rows.values())
        lg = 36 * sum(w for _, w in rows.values()) / tot_min if tot_min else 0.0
        # shrink: 500 pseudo-minutes of league-average defense
        out[s] = {int(pid): 36 * (w + lg * 500 / 36) / (m + 500) for pid, (m, w) in rows.items()}
        out[s]["_league"] = lg
    return out


# --------------------------------------------------------------------------- injuries

def fetch_injuries(ds, log=print, max_age=900):
    """{player_id: {"status", "comment", "team"}} from ESPN's public injury feed (cached 15 min)."""
    cached = _read_cache("injuries.json")
    import time
    if cached and time.time() - cached["fetched"] < max_age:
        raw = cached["rows"]
    else:
        try:
            d = _http_json("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
                           headers={"User-Agent": "Mozilla/5.0"}, retries=2, timeout=20)
            raw = []
            for team in d.get("injuries", []):
                for i in team.get("injuries", []):
                    raw.append({"name": i.get("athlete", {}).get("displayName", ""),
                                "status": i.get("status", ""), "comment": i.get("shortComment") or "",
                                "team": team.get("displayName", "")})
            _write_cache("injuries.json", {"fetched": time.time(), "rows": raw})
        except Exception as e:
            log(f"Injury report unavailable ({e})")
            raw = cached["rows"] if cached else []
    out = {}
    for r in raw:
        p = ds.find_player(r["name"])
        if p:
            st = r["status"].strip().lower()
            out[p.id] = {"status": r["status"], "comment": r["comment"], "name": p.name,
                         "out": st in OUT_STATUSES, "p_out": P_OUT.get(st, 0.25)}
    return out


# --------------------------------------------------------------------------- index

class LineupIndex:
    def __init__(self, ds, walks, expect, defense):
        """expect: {pid: dict of arrays (n+1) exp_min/pts/reb/ast/stocks}, indexed like walk states."""
        self.ds, self.walks, self.expect, self.defense = ds, walks, expect, defense
        self.dates = {pid: [g.date for g in w[0]] for pid, w in walks.items()}
        self.team_dates = defaultdict(set)
        for w in walks.values():
            for g in w[0]:
                self.team_dates[(g.season, g.team)].add(g.date)
        self.team_dates = {k: sorted(v) for k, v in self.team_dates.items()}
        self._hist = None

    @property
    def hist(self):
        """Per team-game summaries for all history (built on first use; only training needs it)."""
        if self._hist is None:
            self._hist = self._build()
        return self._hist

    # ----------------------------------------------------------------- helpers

    def _exp(self, pid, date):
        """Pre-game expectations for a player on `date` (state after their last game before it)."""
        e = self.expect.get(pid)
        if e is None:
            return None
        i = bisect_left(self.dates[pid], date)
        m = e["min"][i]
        if not np.isfinite(m):
            return None
        return {k: float(np.nan_to_num(v[i])) for k, v in e.items()}

    def _def(self, pid, season):
        d = self.defense.get(season - 1) or {}
        return d.get(pid, d.get("_league", 0.08))

    def _team_now(self, pid, date):
        """Team a player belongs to on `date`: the team of their next game (rosters are public pre-game).

        None if they never play again (left the league), so they can't count as missing.
        """
        games = self.walks[pid][0]
        i = bisect_left(self.dates[pid], date)
        return games[i].team if i < len(games) else None

    @staticmethod
    def _summarise(rotation, active, exps, defs, weights=None):
        """Features describing one team in one game.

        weights: optional {pid: probability of sitting} for live games, where the
        injury report gives likelihoods rather than certainties.
        """
        if weights is None:
            weights = {p: 1.0 for p in rotation if p not in active}
        missing = [p for p in rotation if weights.get(p, 0) > 0]
        everyone = set(rotation) | set(active)
        tot = {k: sum(exps[p][k] for p in everyone if p in exps) for k in ("pts", "ast", "reb")}
        miss = {k: sum(weights[p] * exps[p][k] for p in missing if p in exps)
                for k in ("pts", "ast", "reb", "min", "stocks")}
        top = max((p for p in everyone if p in exps), key=lambda p: exps[p]["pts"], default=None)
        act = [p for p in everyone if p in exps and weights.get(p, 0) < 1]
        act_w = {p: 1 - weights.get(p, 0) for p in act}
        act_min = sum(act_w[p] * exps[p]["min"] for p in act)
        return {
            "missing": [p for p in missing if weights[p] >= 0.5], "top": top, "team_pts": tot["pts"],
            "top_out_p": weights.get(top, 0.0) if top is not None else 0.0,
            "missing_min": miss["min"],
            "missing_pts": miss["pts"] / tot["pts"] if tot["pts"] else 0.0,
            "missing_ast": miss["ast"] / tot["ast"] if tot["ast"] else 0.0,
            "missing_reb": miss["reb"] / tot["reb"] if tot["reb"] else 0.0,
            "missing_stocks": miss["stocks"],
            "missing_def": sum(weights[p] * defs.get(p, 0) * exps[p]["min"] / 36 for p in missing if p in exps),
            "active_def": sum(act_w[p] * defs.get(p, 0) * exps[p]["min"] for p in act) / act_min if act_min else np.nan,
        }

    # ----------------------------------------------------------------- history

    def _build(self):
        team_games = defaultdict(set)   # (season, gid, team) -> active pids
        tg_date = {}
        for pid, w in self.walks.items():
            for g in w[0]:
                key = (g.season, g.game_id, g.team)
                team_games[key].add(pid)
                tg_date[key] = g.date
        season_roster = defaultdict(set)
        for (season, _, team), pids in team_games.items():
            season_roster[(season, team)] |= pids

        recent = defaultdict(lambda: deque(maxlen=ROTATION_GAMES))  # (season, team) -> last 10 active sets
        out = {}
        for key in sorted(team_games, key=lambda k: tg_date[k]):
            season, gid, team = key
            date = tg_date[key]
            active = team_games[key]
            history = recent[(season, team)]
            early = len(history) < SEASON_START_GAMES
            appearances = defaultdict(int)
            for s_ in history:
                for p in s_:
                    appearances[p] += 1
            cands = season_roster[(season, team)] if early else set(appearances)
            exps, rotation = {}, []
            for pid in cands | active:
                e = self._exp(pid, date)
                if e is None:
                    continue
                exps[pid] = e
                if pid in active or self._team_now(pid, date) != team:
                    continue
                if early:
                    if e["min"] >= SEASON_START_MIN and bisect_left(self.dates[pid], date) > 0:
                        rotation.append(pid)
                elif e["min"] >= ROTATION_MIN and appearances[pid] >= ROTATION_APPEARANCES:
                    rotation.append(pid)
            rotation += [p for p in active if p in exps and exps[p]["min"] >= ROTATION_MIN]
            defs = {p: self._def(p, season) for p in exps}
            out[(gid, team)] = self._summarise(rotation, active, exps, defs)
            history.append(active)
        return out

    def features(self, pid, game):
        """Lineup features for one historical player-game."""
        own = self.hist.get((game.game_id, game.team))
        opp = self.hist.get((game.game_id, game.opp))
        e = self._exp(pid, game.date)
        return self._row(pid, own, opp, e)

    @staticmethod
    def _row(pid, own, opp, e):
        nan = float("nan")
        r = dict.fromkeys(LINEUP_FEATS, nan)
        if own:
            r.update(tm_missing_min=own["missing_min"], tm_missing_pts=own["missing_pts"],
                     tm_missing_ast=own["missing_ast"], tm_missing_reb=own["missing_reb"],
                     tm_top_out=float(own["top_out_p"]) if own["top"] is not None and own["top"] != pid else 0.0,
                     own_pts_share=(e["pts"] / own["team_pts"]) if e and own["team_pts"] else nan)
        if opp:
            r.update(opp_missing_min=opp["missing_min"], opp_missing_def=opp["missing_def"],
                     opp_missing_stocks=opp["missing_stocks"], opp_missing_reb=opp["missing_reb"],
                     opp_active_def=opp["active_def"])
        return r

    # ----------------------------------------------------------------- live

    def live_team(self, team, date, p_out, season):
        """Summary for a team's next game given who is ruled out."""
        window = set(self.team_dates.get((season, team), [])[-ROTATION_GAMES:])
        early = len(window) < SEASON_START_GAMES
        exps, rotation = {}, []
        for p in self.ds.players.values():
            if not p.on_roster or p.team != team or p.id not in self.walks:
                continue
            e = self._exp(p.id, date)
            if e is None:
                continue
            if early:
                ok = e["min"] >= SEASON_START_MIN
            else:
                apps = sum(1 for g in self.walks[p.id][0][-ROTATION_GAMES:] if g.team == team and g.date in window)
                ok = e["min"] >= ROTATION_MIN and apps >= ROTATION_APPEARANCES
            if ok:
                exps[p.id] = e
                rotation.append(p.id)
        weights = {p: p_out.get(p, 0.0) for p in rotation if p_out.get(p, 0.0) > 0}
        active = {p for p in rotation if weights.get(p, 0) < 1}
        defs = {p: self._def(p, season) for p in exps}
        return self._summarise(rotation, active, exps, defs, weights)

    def live_row(self, pid, team, opp, date, injuries, season):
        p_out = {p: i.get("p_out", 1.0 if i["out"] else 0.0) for p, i in injuries.items()}
        p_out.pop(pid, None)
        own = self.live_team(team, date, p_out, season) if team else None
        oppd = self.live_team(opp, date, p_out, season) if opp else None
        row = self._row(pid, own, oppd, self._exp(pid, date))
        if not opp:
            # neutral opponent: nobody missing, league-average defenders
            lg = (self.defense.get(season - 1) or {}).get("_league", 0.08)
            row.update(opp_missing_min=0.0, opp_missing_def=0.0, opp_missing_stocks=0.0,
                       opp_missing_reb=0.0, opp_active_def=lg)
        return row, own, oppd
