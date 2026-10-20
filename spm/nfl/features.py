"""Leak-free NFL features: every row is built only from what was known before kickoff.

Games are processed in date order. For each game, every player's features are read from
running state (player form, team volume, opponent defense, who's missing), and only then is
the state updated with that game's results. A team plays once a week, so nothing from a
game, or from a later game, reaches its own features.

Also used live: `FeatureBuilder.live_row()` builds the same features for an upcoming game
from the state after the last completed week.
"""

import math
from collections import defaultdict

from . import data

HL_SHORT, HL_LONG, HL_TEAM, HL_DEF = 3.0, 10.0, 6.0, 8.0     # half-lives, in games
BASE = ("pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "rush_att", "rush_yds", "rush_td",
        "rec", "rec_yds", "rec_td", "targets", "any_td")
SHARES = ("tgt_share", "carry_share", "pass_share", "snap")
POSITIONS = ("QB", "RB", "WR", "TE")
DEF_STATS = BASE
# injury-report status -> chance the player sits (live "who's missing")
SIT_PROB = {"Out": 1.0, "Doubtful": 0.85, "Questionable": 0.3}
NAN = float("nan")

FEATURES = (
    [f"pos_{p}" for p in POSITIONS] + ["n_games", "games_missed", "season_games"]
    + [f"{s}_{k}" for s in BASE for k in ("s", "l", "szn", "prev")]
    + [f"{s}_{k}" for s in SHARES for k in ("s", "l")] + ["snap_last"]
    + ["ypt", "ypc", "catch_rate", "ypa", "cmp_rate", "td_per_touch", "int_rate", "pass_td_rate"]
    + ["team_pass_att", "team_rush_att", "team_pts", "opp_pts_allowed"]
    + ["home", "spread", "implied_pts", "game_total", "rest", "opp_rest", "dome", "wind", "temp",
       "div_game", "week", "post"]
    + [f"opp_allow_{s}" for s in DEF_STATS]
    + ["vac_tgt", "vac_carry", "vac_pass"]
)
# Feature groups for explanations (reasoning shows each group's effect).
GROUPS = {
    "Recent form": [f for f in FEATURES if f.endswith(("_s", "_l", "_szn", "_prev")) and not
                    f.startswith(tuple(SHARES))] + ["snap_last"],
    "Role & usage": [f"{s}_{k}" for s in SHARES for k in ("s", "l")] + ["n_games", "season_games"],
    "Efficiency": ["ypt", "ypc", "catch_rate", "ypa", "cmp_rate", "td_per_touch", "int_rate", "pass_td_rate"],
    "Game script (spread & total)": ["spread", "implied_pts", "game_total", "team_pts", "team_pass_att",
                                     "team_rush_att"],
    "Opponent defense": [f"opp_allow_{s}" for s in DEF_STATS] + ["opp_pts_allowed"],
    "Teammates out": ["vac_tgt", "vac_carry", "vac_pass"],
    "Injury return": ["games_missed"],
    "Venue & weather": ["home", "dome", "wind", "temp", "div_game"],
    "Rest & schedule": ["rest", "opp_rest", "week", "post"],
    "Position": [f"pos_{p}" for p in POSITIONS],
}


class EWM:
    """Exponentially weighted mean over games (half-life in games)."""
    __slots__ = ("d", "s", "w")

    def __init__(self, half_life):
        self.d, self.s, self.w = 0.5 ** (1 / half_life), 0.0, 0.0

    def add(self, x):
        self.s, self.w = self.s * self.d + x, self.w * self.d + 1

    def mean(self):
        return self.s / self.w if self.w else NAN


def _div(a, b):
    return a / b if b and b > 0 and not math.isnan(a) and not math.isnan(b) else NAN


def value(row, stat):
    return row["rush_td"] + row["rec_td"] + row["other_td"] if stat == "any_td" else row[stat]


class Player:
    def __init__(self):
        self.ewm = {(s, k): EWM(hl) for s in (*BASE, *SHARES) for k, hl in (("s", HL_SHORT), ("l", HL_LONG))}
        self.n = 0
        self.season, self.szn_sum, self.szn_n, self.prev = None, defaultdict(float), 0, {}
        self.team, self.team_game_idx, self.snap_last = None, None, NAN
        self.name, self.position, self.last_game = "", "", ""


class FeatureBuilder:
    """Walks games in order; `rows` must be data.load_player_games() output (sorted by date)."""

    def __init__(self, participation=None):
        self.players = defaultdict(Player)
        self.team_ewm = defaultdict(lambda: {k: EWM(HL_TEAM) for k in ("pass_att", "rush_att", "targets", "pts",
                                                                       "pts_allowed")})
        self.team_games = defaultdict(int)       # team -> games played so far
        self.defense = defaultdict(lambda: {s: EWM(HL_DEF) for s in DEF_STATS})   # (def team, pos) -> allowed
        self.participation = participation or {}
        self.regulars = defaultdict(dict)        # team -> {player key: (season, team game index last played)}

    # ------------------------------------------------------------------ features
    def _player_feats(self, p, pos, season):
        f = {f"pos_{q}": 1.0 if pos == q else 0.0 for q in POSITIONS}
        f["n_games"] = float(min(p.n, 50))
        same = p.season == season
        f["season_games"] = float(p.szn_n if same else 0)
        prev = p.prev if same else ({s: p.szn_sum[s] / p.szn_n for s in BASE} if p.szn_n else {})
        for s in BASE:
            f[f"{s}_s"], f[f"{s}_l"] = p.ewm[(s, "s")].mean(), p.ewm[(s, "l")].mean()
            f[f"{s}_szn"] = p.szn_sum[s] / p.szn_n if same and p.szn_n else NAN
            f[f"{s}_prev"] = prev.get(s, NAN)
        for s in SHARES:
            f[f"{s}_s"], f[f"{s}_l"] = p.ewm[(s, "s")].mean(), p.ewm[(s, "l")].mean()
        f["snap_last"] = p.snap_last
        L = lambda s: p.ewm[(s, "l")].mean()
        f["ypt"] = _div(L("rec_yds"), L("targets"))
        f["ypc"] = _div(L("rush_yds"), L("rush_att"))
        f["catch_rate"] = _div(L("rec"), L("targets"))
        f["ypa"] = _div(L("pass_yds"), L("pass_att"))
        f["cmp_rate"] = _div(L("pass_cmp"), L("pass_att"))
        f["td_per_touch"] = _div(L("rush_td") + L("rec_td"), L("rush_att") + L("targets"))
        f["int_rate"] = _div(L("pass_int"), L("pass_att"))
        f["pass_td_rate"] = _div(L("pass_td"), L("pass_att"))
        return f

    def _context_feats(self, ctx, team, opp, pos, week, post):
        t, o = self.team_ewm[team], self.team_ewm[opp]
        f = {"team_pass_att": t["pass_att"].mean(), "team_rush_att": t["rush_att"].mean(),
             "team_pts": t["pts"].mean(), "opp_pts_allowed": o["pts_allowed"].mean(),
             "week": float(week), "post": 1.0 if post else 0.0}
        for k in ("home", "spread", "implied_pts", "game_total", "rest", "opp_rest", "dome", "wind", "temp",
                  "div_game"):
            v = ctx.get(k)
            f[k] = NAN if v is None else float(v)
        d = self.defense[(opp, pos)]
        for s in DEF_STATS:
            f[f"opp_allow_{s}"] = d[s].mean()
        return f

    def _vacated(self, team, season, active, sit_prob=None):
        """Recent target/carry/pass share of the team's regulars who aren't playing.

        History: `active` = players who took a snap. Live: `sit_prob` = {key: chance of sitting}.
        """
        idx = self.team_games[team]
        vac = {"vac_tgt": 0.0, "vac_carry": 0.0, "vac_pass": 0.0}
        for key, (szn, last) in self.regulars[team].items():
            # a current regular: played for the team this season, in one of its last two games
            # (offseason departures are roster turnover, not missing players)
            if szn != season or idx - last > 2:
                continue
            p = self.players[key]
            if sit_prob is not None:
                w = sit_prob.get(key, 0.0)
            else:
                w = 0.0 if key in active else 1.0
            if w:
                vac["vac_tgt"] += w * _nz(p.ewm[("tgt_share", "s")].mean())
                vac["vac_carry"] += w * _nz(p.ewm[("carry_share", "s")].mean())
                vac["vac_pass"] += w * _nz(p.ewm[("pass_share", "s")].mean())
        return vac

    def row_features(self, key, pos, team, opp, season, week, post, ctx, vac):
        p = self.players[key]
        f = self._player_feats(p, pos, season)
        f.update(self._context_feats(ctx, team, opp, pos, week, post))
        missed = self.team_games[team] - p.team_game_idx - 1 if p.team == team and p.team_game_idx is not None else 0
        f["games_missed"] = float(min(max(missed, 0), 8))
        f.update(vac)
        return f

    # ------------------------------------------------------------------ walk
    def walk(self, rows):
        """Yield (row, features) for every player-game, oldest first."""
        game = []
        for r in rows:
            if game and r["game_id"] != game[0]["game_id"]:
                yield from self._game(game)
                game = []
            game.append(r)
        if game:
            yield from self._game(game)

    def _game(self, rows):
        g = rows[0]["game_id"]
        by_team = defaultdict(list)
        for r in rows:
            by_team[r["team"]].append(r)
        vac = {}
        for team in by_team:
            active = self.participation.get((g, team)) or {r["key"] for r in by_team[team]}
            vac[team] = self._vacated(team, by_team[team][0]["season"], active)
        out = []
        for r in rows:
            f = self.row_features(r["key"], r["position"], r["team"], r["opp"], r["season"], r["week"], r["post"],
                                  r, vac[r["team"]])
            out.append((r, f))
        self._update(g, by_team)
        return out

    def _update(self, game_id, by_team):
        totals = {}
        for team, rs in by_team.items():
            totals[team] = {s: sum(r[s] for r in rs) for s in ("pass_att", "rush_att", "targets")}
        for team, rs in by_team.items():
            opp = rs[0]["opp"]
            tot = totals[team]
            pts = _team_points(rs[0])
            self.team_ewm[team]["pass_att"].add(tot["pass_att"])
            self.team_ewm[team]["rush_att"].add(tot["rush_att"])
            self.team_ewm[team]["targets"].add(tot["targets"])
            if pts is not None:
                self.team_ewm[team]["pts"].add(pts[0])
                self.team_ewm[team]["pts_allowed"].add(pts[1])
            # what this team's offense did, by position, is what the opponent's defense allowed
            by_pos = defaultdict(lambda: defaultdict(float))
            for r in rs:
                if r["position"] in POSITIONS:
                    for s in DEF_STATS:
                        by_pos[r["position"]][s] += value(r, s)
            for pos in POSITIONS:
                for s in DEF_STATS:
                    self.defense[(opp, pos)][s].add(by_pos[pos][s])
            idx = self.team_games[team]
            for r in rs:
                p = self.players[r["key"]]
                if p.season != r["season"]:
                    if p.szn_n:
                        p.prev = {s: p.szn_sum[s] / p.szn_n for s in BASE}
                    p.season, p.szn_sum, p.szn_n = r["season"], defaultdict(float), 0
                shares = {"tgt_share": _div(r["targets"], tot["targets"]),
                          "carry_share": _div(r["rush_att"], tot["rush_att"]),
                          "pass_share": _div(r["pass_att"], tot["pass_att"]),
                          "snap": r["snap_pct"] if r["snap_pct"] is not None else NAN}
                for s in BASE:
                    v = value(r, s)
                    p.ewm[(s, "s")].add(v)
                    p.ewm[(s, "l")].add(v)
                    p.szn_sum[s] += v
                for s, v in shares.items():
                    if not math.isnan(v):
                        p.ewm[(s, "s")].add(v)
                        p.ewm[(s, "l")].add(v)
                p.snap_last = shares["snap"]
                p.szn_n += 1
                p.n += 1
                p.team, p.team_game_idx = team, idx
                p.name, p.position, p.last_game = r["name"], r["position"], r["gameday"]
                if r["position"] in POSITIONS:
                    self.regulars[team][r["key"]] = (r["season"], idx)
            self.team_games[team] += 1

    # ------------------------------------------------------------------ live
    def live_row(self, key, pos, team, opp, season, week, ctx, injuries):
        """Features for an upcoming game, from the state after all completed games.

        injuries: {(team, key): report status} for this week.
        """
        sit = {k: SIT_PROB.get(st, 0.0) for (t, k), st in injuries.items() if t == team}
        sit.pop(key, None)                      # the player himself is assumed to play (else the bet is void)
        return self.row_features(key, pos, team, opp, season, week, False, ctx,
                                 self._vacated(team, season, (), sit))


def _nz(v):
    return 0.0 if math.isnan(v) else v


_GAMES = {}


def set_games(games):
    """Schedule (data.load_games()) used to look up final scores for team points."""
    _GAMES.clear()
    _GAMES.update(games)


def _team_points(row):
    g = _GAMES.get(row["game_id"])
    if not g or g["home_score"] is None:
        return None
    home = row["team"] == g["home"]
    return (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])


def build(rows=None, log=print):
    """All historical rows with features: [(row, features dict)]."""
    rows = rows if rows is not None else data.load_player_games()
    set_games(data.load_games())
    fb = FeatureBuilder(data.load_participation())
    out = list(fb.walk(rows))
    log(f"Built features for {len(out):,} player-games")
    return out, fb
