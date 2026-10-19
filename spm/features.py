"""Feature engine for the ML model.

Walks every player's career once, carrying exponentially-weighted running sums
(half-lives 10/25/40/100 games) of minutes and every stat. Each game's feature
row is built only from what was known before tip-off:

* the player's recent and long-run per-minute rates, per-game averages, spread,
  season-to-date average, minutes trend, rest, time with current team, experience;
* how much of each stat the opponent had allowed *up to that date* (blended
  with its previous season);
* previous-season shot-zone and play-type matchup edges (see advanced.py).

The same code produces the "next game" row used for live predictions, so
training and live features are computed identically.
"""

import datetime as dt
from bisect import bisect_left

import numpy as np

from .lineups import LINEUP_FEATS, LINEUP_LABELS, LineupIndex
from .model import DISP_CAP, STATS

KEYS = list(STATS)
K = len(KEYS)
HL = np.array([10.0, 25.0, 40.0, 100.0])
DEC = 0.5 ** (1 / HL)
I10, I25, I40, I100 = range(4)
C_MIN, C_ONE, C_VAL, C_SQ = 0, 1, 2, 2 + K
NCOL = 2 + 2 * K
MIN_HISTORY = 8
REST_CAP = 4      # days; longer gaps within a season are captured by missed_games instead
OPENER_REST = 3   # a season opener is treated as normal rest, not a 4-month layoff

ZONE_FEATS = ["three_share", "rim_share", "zone_fg_edge", "zone_freq_edge", "opp_3p_delta",
              "opp_3pa_ratio", "opp_rim_delta"]
PT_FEATS = ["pt_iso", "pt_prbh", "pt_spot", "pt_trans", "pt_post", "pt_roll", "pt_eff",
            "pt_ppp_edge", "pt_freq_edge"]
SHARED = (["n_hist", "prior_share", "experience", "home", "rest", "b2b", "missed_games", "games_with_team",
           "days_into_season", "min_10", "min_25", "min_40", "min_100",
           "pts_rate", "reb_rate", "ast_rate", "fg3m_rate", "stl_rate", "blk_rate", "tov_rate",
           "opp_pts_ratio"] + ZONE_FEATS + PT_FEATS + LINEUP_FEATS)
STAT_FEATS = ["rate_10", "rate_25", "rate_40", "rate_100", "avg_10", "avg_40", "disp_40",
              "szn_avg", "szn_n", "opp_ratio", "base"]
FEATURES = SHARED + STAT_FEATS

FEATURE_LABELS = {
    "n_hist": "Games of history", "prior_share": "Reliance on last season", "experience": "Experience",
    "home": "Home/road", "rest": "Days of rest", "b2b": "Back-to-back",
    "missed_games": "Games missed recently", "games_with_team": "Time with current team",
    "days_into_season": "Point in season", "min_10": "Recent minutes", "min_25": "Minutes (25-game)",
    "min_40": "Minutes (40-game)", "min_100": "Long-run minutes", "pts_rate": "Scoring rate",
    "reb_rate": "Rebounding rate", "ast_rate": "Assist rate", "fg3m_rate": "Three-point rate",
    "stl_rate": "Steal rate", "blk_rate": "Block rate", "tov_rate": "Turnover rate",
    "opp_pts_ratio": "Opponent points allowed", "three_share": "Three-point shot share",
    "rim_share": "Rim shot share", "zone_fg_edge": "Shot-zone matchup (FG%)",
    "zone_freq_edge": "Shot-zone matchup (volume)", "opp_3p_delta": "Opponent 3P% allowed",
    "opp_3pa_ratio": "Opponent 3PA allowed", "opp_rim_delta": "Opponent rim FG% allowed",
    "pt_iso": "Isolation usage", "pt_prbh": "Pick-and-roll usage", "pt_spot": "Spot-up usage",
    "pt_trans": "Transition usage", "pt_post": "Post-up usage", "pt_roll": "Roll-man usage",
    "pt_eff": "Play-type efficiency", "pt_ppp_edge": "Play-type matchup (efficiency)",
    "pt_freq_edge": "Play-type matchup (frequency)", "rate_10": "Last ~10 games per-minute",
    "rate_25": "Recent per-minute", "rate_40": "Per-minute (40-game)", "rate_100": "Long-run per-minute",
    "avg_10": "Recent per-game average", "avg_40": "Per-game average (40-game)", "disp_40": "Game-to-game volatility",
    "szn_avg": "Season-to-date average", "szn_n": "Games this season", "opp_ratio": "Opponent allows this stat",
    "base": "Baseline projection",
    **LINEUP_LABELS,
}
ADV_FEATS = ZONE_FEATS + PT_FEATS + LINEUP_FEATS


def _values(g):
    vals = [float(sum(getattr(g, f) for f in STATS[k][1])) for k in KEYS]
    return [g.min, 1.0] + vals + [v * v for v in vals]


def _season_day(date, season):
    return (dt.date.fromisoformat(date) - dt.date(season, 10, 1)).days


# --------------------------------------------------------------------------- opponent index

class OppIndex:
    """What each defense allowed, queryable as of any date."""

    def __init__(self, team_games):
        # team_games: list of (season, date, defense_team, values[K])
        by_def, lg = {}, {}
        for season, date, team, vals in sorted(team_games, key=lambda x: x[1]):
            by_def.setdefault((season, team), ([], []))
            by_def[(season, team)][0].append(date)
            by_def[(season, team)][1].append(vals)
            lg.setdefault(season, ([], []))
            lg[season][0].append(date)
            lg[season][1].append(vals)

        def cum(entry):
            arr = np.asarray(entry[1], dtype=np.float64)
            return entry[0], np.vstack([np.zeros((1, K)), np.cumsum(arr, axis=0)])

        self.by_def = {k: cum(v) for k, v in by_def.items()}
        self.lg = {k: cum(v) for k, v in lg.items()}
        self.full = {}
        for (season, team), (dates, c) in self.by_def.items():
            ldates, lc = self.lg[season]
            self.full[(season, team)] = (c[-1] / len(dates)) / np.maximum(lc[-1] / len(ldates), 1e-9)

    def ratio(self, season, team, date):
        prev = self.full.get((season - 1, team))
        e = self.by_def.get((season, team))
        n = bisect_left(e[0], date) if e else 0
        cur = None
        if n:
            ldates, lc = self.lg[season]
            j = bisect_left(ldates, date)
            cur = (e[1][n] / n) / np.maximum(lc[j] / j, 1e-9)
        if cur is not None and prev is not None:
            w = n / (n + 15)
            return w * cur + (1 - w) * prev
        return cur if cur is not None else prev

    def ratio_or_nan(self, season, team, date):
        if not team:
            return np.ones(K)
        r = self.ratio(season, team, date)
        return r if r is not None else np.full(K, np.nan)


# --------------------------------------------------------------------------- state -> features

def _state_features(P):
    """P: (m, 4, NCOL) weighted sums -> dict of feature arrays."""
    with np.errstate(divide="ignore", invalid="ignore"):
        W = P[:, :, C_ONE]
        M = P[:, :, C_MIN]
        V = P[:, :, C_VAL:C_VAL + K]
        S2 = P[:, :, C_SQ:C_SQ + K]
        rate = V / M[:, :, None]
        avg = V / W[:, :, None]
        mins = M / W
        mean40 = avg[:, I40]
        var40 = S2[:, I40] / W[:, I40, None] - mean40 ** 2
        disp = var40 / mean40
    caps = np.array([DISP_CAP[k] for k in KEYS])
    disp = np.clip(np.nan_to_num(disp, nan=1.0), 1.0, caps)
    return {"rate": rate, "avg": avg, "mins": mins, "disp": disp, "w25": W[:, I25]}


def compose(st, ctx, adv_rows):
    """Build the shared matrix and per-stat matrices.

    st: output of _state_features (length m); ctx: dict of length-m arrays;
    adv_rows: list of m dicts with zone + play-type features.
    """
    m = st["rate"].shape[0]
    rate, avg, mins = st["rate"], st["avg"], st["mins"]
    kidx = {k: i for i, k in enumerate(KEYS)}
    opp = ctx["opp_ratio"]  # (m, K)
    shared = np.column_stack([
        np.minimum(ctx["n_hist"], 400), ctx["prior_share"], ctx["experience"], ctx["home"], ctx["rest"],
        ctx["b2b"], np.minimum(ctx["missed"], 20), np.minimum(ctx["gwt"], 82), ctx["days_into"],
        mins[:, I10], mins[:, I25], mins[:, I40], mins[:, I100],
        rate[:, I25, kidx["PTS"]], rate[:, I25, kidx["REB"]], rate[:, I25, kidx["AST"]],
        rate[:, I25, kidx["3PM"]], rate[:, I25, kidx["STL"]], rate[:, I25, kidx["BLK"]],
        rate[:, I25, kidx["TOV"]], opp[:, kidx["PTS"]],
        np.array([[r.get(f) if r.get(f) is not None else np.nan for f in ADV_FEATS]
                  for r in adv_rows], dtype=np.float64).reshape(m, len(ADV_FEATS)),
    ]).astype(np.float32)

    min_mix = 0.6 * mins[:, I10] + 0.4 * mins[:, I40]
    per_stat, base = {}, {}
    for k, i in kidx.items():
        oppk = opp[:, i]
        b = (0.7 * rate[:, I25, i] + 0.3 * rate[:, I100, i]) * min_mix * (1 + 0.5 * (np.nan_to_num(oppk, nan=1.0) - 1))
        base[k] = b
        per_stat[k] = np.column_stack([
            rate[:, I10, i], rate[:, I25, i], rate[:, I40, i], rate[:, I100, i],
            avg[:, I10, i], avg[:, I40, i], st["disp"][:, i],
            # no games yet this season -> fall back to the recent average (szn_n = 0 still flags it)
            np.where(np.isnan(ctx["szn_avg"][:, i]), avg[:, I40, i], ctx["szn_avg"][:, i]), ctx["szn_n"], oppk, b,
        ]).astype(np.float32)
    return shared, per_stat, base


# --------------------------------------------------------------------------- per player walk

def _walk(p, current_season=None):
    """Pre-game state for each game t (index t) and for the next game (index n)."""
    games = [g for g in p.games if g.min >= 1]
    n = len(games)
    if n == 0:
        return None
    X = np.array([_values(g) for g in games])
    P = np.zeros((n + 1, 4, NCOL))
    for t in range(n):
        P[t + 1] = DEC[:, None] * P[t] + X[t]

    d25 = DEC[I25]
    cs = np.zeros(n + 1)            # this season's share of the hl-25 weight
    szn_sum = np.zeros((n + 1, K))  # season-to-date totals
    szn_n = np.zeros(n + 1)
    gwt = np.zeros(n + 1)           # consecutive games with the current team
    rest = np.full(n + 1, np.nan)
    run_season = run_team = None
    run_n = run_cs = run_gwt = 0.0
    run_sum = np.zeros(K)
    for t in range(1, n + 1):
        prev = games[t - 1]
        if prev.season != run_season:
            run_season, run_n, run_cs, run_sum = prev.season, 0.0, 0.0, np.zeros(K)
        run_n += 1
        run_cs = d25 * run_cs + 1
        run_sum = run_sum + X[t - 1, C_VAL:C_VAL + K]
        if prev.team != run_team:
            run_team, run_gwt = prev.team, 0.0
        run_gwt += 1
        nxt_season = games[t].season if t < n else current_season
        nxt_team = games[t].team if t < n else (p.team or prev.team)
        if nxt_season == run_season:
            cs[t], szn_n[t], szn_sum[t] = run_cs, run_n, run_sum
        gwt[t] = run_gwt if nxt_team == run_team else 0.0
        if t < n:
            if games[t].season != prev.season:
                rest[t] = OPENER_REST
            else:
                gap = (dt.date.fromisoformat(games[t].date) - dt.date.fromisoformat(prev.date)).days
                rest[t] = min(gap, REST_CAP)
    return games, X, P, cs, szn_sum, szn_n, gwt, rest


def _ctx(seasons, dates, homes, teams_opp, P, cs, szn_sum, szn_n, gwt, rest, missed, n_hist,
         debut, oppidx):
    with np.errstate(divide="ignore", invalid="ignore"):
        szn_avg = np.where(szn_n[:, None] > 0, szn_sum / np.maximum(szn_n[:, None], 1), np.nan)
        prior_share = 1 - cs / P[:, I25, C_ONE]
    exp = np.array([s - debut if debut else np.nan for s in seasons], dtype=np.float64)
    return {
        "n_hist": n_hist, "prior_share": prior_share, "experience": exp, "home": homes,
        "rest": rest, "b2b": (rest == 1).astype(np.float64), "missed": missed, "gwt": gwt,
        "days_into": np.array([_season_day(d, s) for d, s in zip(dates, seasons)], dtype=np.float64),
        "szn_avg": szn_avg, "szn_n": szn_n,
        "opp_ratio": np.array([oppidx.ratio_or_nan(s, o, d) for s, o, d in zip(seasons, teams_opp, dates)]),
    }


class FeatureStore:
    """Training frame for all games, plus each player's next-game state for live use."""

    def __init__(self, ds, profiles, build_training=True, log=print):
        self.ds, self.prof = ds, profiles
        self.walks = {}
        team_games = {}
        for p in ds.players.values():
            w = _walk(p, ds.current_start)
            if w is None:
                continue
            self.walks[p.id] = w
            games, X = w[0], w[1]
            for g, x in zip(games, X[:, C_VAL:C_VAL + K]):
                key = (g.season, g.game_id, g.team)
                e = team_games.get(key)
                if e is None:
                    team_games[key] = [g.season, g.date, g.opp, x.copy()]
                else:
                    e[3] += x
        self.opp = OppIndex([tuple(v) for v in team_games.values()])
        self.team_dates = {}
        for season, gid, team in team_games:
            self.team_dates.setdefault((season, team), set()).add(team_games[(season, gid, team)][1])
        self.team_dates = {k: sorted(v) for k, v in self.team_dates.items()}
        self.missed = {pid: self._missed_games(w[0]) for pid, w in self.walks.items()}
        self._adv_cache = {}
        self.lineups = LineupIndex(ds, self.walks, self._expectations(), profiles.defense)
        self.train = self._training_frame(log) if build_training else None

    # ----------------------------------------------------------------- helpers

    def _team_games_between(self, season, team, after, before):
        """Team games strictly between two dates (games a player sat out)."""
        dates = self.team_dates.get((season, team), [])
        return max(0, bisect_left(dates, before) - bisect_left(dates, after) - 1) if after < before else 0

    def _missed_games(self, games):
        out = np.zeros(len(games) + 1)
        for t in range(1, len(games)):
            g, prev = games[t], games[t - 1]
            if g.season == prev.season:
                out[t] = self._team_games_between(g.season, g.team, prev.date, g.date)
            else:  # first game of a season: team games already played without him
                out[t] = bisect_left(self.team_dates.get((g.season, g.team), []), g.date)
        return out

    def _expectations(self):
        """Pre-game expected minutes and per-game stats for every player state (used for lineups)."""
        kidx = {k: i for i, k in enumerate(KEYS)}
        out = {}
        for pid, w in self.walks.items():
            st = _state_features(w[2])
            avg = st["avg"][:, I25]
            out[pid] = {"min": 0.6 * st["mins"][:, I10] + 0.4 * st["mins"][:, I40],
                        "pts": avg[:, kidx["PTS"]], "reb": avg[:, kidx["REB"]], "ast": avg[:, kidx["AST"]],
                        "stocks": avg[:, kidx["SB"]]}
        return out

    def adv(self, pid, opp, season):
        key = (pid, opp, season)
        r = self._adv_cache.get(key)
        if r is None:
            r = self.prof.zone_features(pid, opp, season) | self.prof.playtype_features(pid, opp, season)
            self._adv_cache[key] = r
        return r

    def _training_frame(self, log):
        parts = {"shared": [], "stat": {k: [] for k in KEYS}, "base": {k: [] for k in KEYS},
                 "y": {k: [] for k in KEYS}, "pid": [], "season": [], "date": [], "min": [], "infl": []}
        for pid, (games, X, P, cs, szn_sum, szn_n, gwt, rest) in self.walks.items():
            n = len(games)
            idx = [t for t in range(n) if t >= MIN_HISTORY and games[t].season > self.ds_first_season()]
            if not idx:
                continue
            idx = np.array(idx)
            p = self.ds.players[pid]
            seasons = [games[t].season for t in idx]
            dates = [games[t].date for t in idx]
            st = _state_features(P[idx])
            ctx = _ctx(seasons, dates, np.array([float(games[t].home) for t in idx]),
                       [games[t].opp for t in idx], P[idx], cs[idx], szn_sum[idx], szn_n[idx],
                       gwt[idx], rest[idx], self.missed[pid][idx], idx.astype(np.float64), p.from_year, self.opp)
            adv_rows = [self.adv(pid, games[t].opp, games[t].season) | self.lineups.features(pid, games[t])
                        for t in idx]
            shared, per_stat, base = compose(st, ctx, adv_rows)
            parts["shared"].append(shared)
            for i, k in enumerate(KEYS):
                parts["stat"][k].append(per_stat[k])
                parts["base"][k].append(base[k])
                parts["y"][k].append(X[idx, C_VAL + i].astype(np.float32))
            parts["pid"].append(np.full(len(idx), pid))
            parts["season"].append(np.array(seasons))
            parts["date"].append(np.array(dates))
            parts["min"].append(np.array([games[t].min for t in idx], dtype=np.float32))
            parts["infl"].append(_inflation(ctx))
        cat = np.concatenate
        frame = {
            "shared": cat(parts["shared"]), "pid": cat(parts["pid"]), "season": cat(parts["season"]),
            "date": cat(parts["date"]), "min": cat(parts["min"]), "infl": cat(parts["infl"]),
            "stat": {k: cat(v) for k, v in parts["stat"].items()},
            "base": {k: cat(v) for k, v in parts["base"].items()},
            "y": {k: cat(v) for k, v in parts["y"].items()},
        }
        log(f"Training frame: {len(frame['pid']):,} player-games, {len(FEATURES)} features per stat")
        return frame

    def ds_first_season(self):
        return min(self.ds.league_avg)

    # ----------------------------------------------------------------- live

    def live(self, pid, opponent=None, home=None, b2b=False, date=None, typical=None, injuries=None):
        """Feature row for a player's next game. Returns (X dict per stat, info) or None.

        typical: a dict of league-median context values (from training) describing an
        ordinary mid-season game, for season-long per-game projections.
        """
        w = self.walks.get(pid)
        if w is None:
            return None
        games, X, P, cs, szn_sum, szn_n, gwt, rest = w
        n = len(games)
        if n < MIN_HISTORY:
            return None
        p = self.ds.players[pid]
        season = self.ds.current_start
        date = date or dt.date.today().isoformat()
        last = games[-1]
        if b2b:
            r, missed = 1.0, 0.0
        elif last.season < season:
            r = float(OPENER_REST)
            missed = float(bisect_left(self.team_dates.get((season, p.team), []), date))
        else:
            r = float(min(REST_CAP, max(2, (dt.date.fromisoformat(date) - dt.date.fromisoformat(last.date)).days)))
            missed = float(self._team_games_between(season, p.team, last.date, date))
        sl = slice(n, n + 1)
        st = _state_features(P[sl])
        ctx = _ctx([season], [date], np.array([np.nan if home is None else float(home)]),
                   [opponent], P[sl], cs[sl], szn_sum[sl], szn_n[sl], gwt[sl], np.array([r]),
                   np.array([missed]), np.array([float(n)]), p.from_year, self.opp)
        if typical:
            ctx["days_into"] = np.maximum(ctx["days_into"], 90.0)
            ctx["rest"] = np.array([2.0])
            ctx["b2b"] = np.array([0.0])
            ctx["missed"] = np.array([0.0])
            ctx["gwt"] = np.maximum(ctx["gwt"], 20.0)
            ctx["szn_n"] = np.maximum(ctx["szn_n"], 20.0)
            ctx["prior_share"] = np.minimum(ctx["prior_share"], typical.get("prior_share", 0.25))
        lineup, own, opp_team = self.lineups.live_row(pid, p.team, opponent, date, injuries or {}, season)
        if typical:
            lineup = {**lineup, **{k: v for k, v in typical.items() if k in lineup and k not in ("own_pts_share", "tm_top_out")}}
        adv = self.adv(pid, opponent, season) | lineup
        shared, per_stat, base = compose(st, ctx, [adv])
        info = {"disp": st["disp"][0], "infl": float(_inflation(ctx)[0]), "adv": adv,
                "opp_ratio": ctx["opp_ratio"][0], "base": {k: float(base[k][0]) for k in KEYS},
                "min_10": float(st["mins"][0, I10]), "min_40": float(st["mins"][0, I40]),
                "lineup": lineup, "own_team": own, "opp_team": opp_team}
        return {k: np.hstack([shared, per_stat[k]]) for k in KEYS}, info


def _inflation(ctx):
    infl = np.ones(len(ctx["n_hist"]))
    infl *= np.where(ctx["gwt"] < 5, 1.2, 1.0)
    infl *= np.where(ctx["prior_share"] > 0.8, 1.08, 1.0)
    infl *= np.where(ctx["n_hist"] < 30, 1.1, 1.0)
    return infl
