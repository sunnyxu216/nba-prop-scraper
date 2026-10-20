"""NFL features and outcome distributions (spm/nfl/features.py, spm/nfl/model.py), on synthetic data."""

import math

import numpy as np
import pytest

from spm.nfl import features as F
from spm.nfl import model as M


def game_row(week, key="a b", team="KC", opp="LV", rec=5, rec_yds=60, targets=7, pos="WR", season=2025, **stats):
    base = dict.fromkeys(("pass_att", "pass_cmp", "pass_yds", "pass_td", "pass_int", "rush_att", "rush_yds",
                          "rush_td", "rec_td", "other_td"), 0.0)
    return {"player_id": key, "name": key, "key": key, "position": pos, "team": team, "opp": opp,
            "season": season, "week": week, "game_id": f"{season}_{week:02d}_{team}", "gameday": f"{season}-10-{week:02d}",
            "post": False, **base, "rec": float(rec), "rec_yds": float(rec_yds), "targets": float(targets),
            "target_share": 0.2, "air_yards_share": 0.2, "snap_pct": 0.8, "home": 1.0, "spread": 3.0,
            "game_total": 47.0, "implied_pts": 25.0, "rest": 7.0, "opp_rest": 7.0, "dome": 0.0, "wind": 5.0,
            "temp": 60.0, "div_game": 0.0, **stats}


def walk(rows):
    return list(F.FeatureBuilder().walk(rows))


# --------------------------------------------------------------------------- features

def test_features_are_leak_free():
    """A game's own outcome never reaches its features."""
    rows = [game_row(w) for w in (1, 2, 3)]
    changed = rows[:2] + [game_row(3, rec=15, rec_yds=250, targets=20)]
    a, b = walk(rows)[-1][1], walk(changed)[-1][1]
    assert all((math.isnan(a[k]) and math.isnan(b[k])) or a[k] == b[k] for k in F.FEATURES)


def test_first_game_has_no_history():
    (_, f), = walk([game_row(1)])
    assert f["n_games"] == 0 and math.isnan(f["rec_s"]) and f["season_games"] == 0


def test_recent_form_and_season_average():
    out = walk([game_row(1, rec=2), game_row(2, rec=4), game_row(3, rec=9)])
    f = out[-1][1]
    assert f["rec_szn"] == 3.0 and f["n_games"] == 2
    assert 3.0 < f["rec_s"] < 4.0          # recency-weighted: closer to the latest game


def test_new_season_resets_season_average_and_keeps_previous():
    out = walk([game_row(1, rec=4, season=2024), game_row(2, rec=6, season=2024), game_row(1, rec=1, season=2025)])
    f = out[-1][1]
    assert math.isnan(f["rec_szn"]) and f["rec_prev"] == 5.0 and f["season_games"] == 0


def test_vacated_targets_when_a_regular_misses_a_game():
    star = lambda w: game_row(w, key="star", targets=10)
    me = lambda w: game_row(w, key="me", targets=3)
    out = walk([star(1), me(1), star(2), me(2), me(3)])      # star misses week 3
    f = out[-1][1]
    assert f["vac_tgt"] > 0.5                                   # ~77% of the team's targets freed up
    assert out[2][1]["vac_tgt"] == 0                            # week 2: everyone played


def test_opponent_defense_is_what_opponents_gained():
    rows = [game_row(1, key="x", team="KC", opp="LV", rec_yds=100),
            game_row(2, key="y", team="DEN", opp="LV", rec_yds=50),
            game_row(3, key="z", team="NE", opp="LV", rec_yds=0)]
    f = walk(rows)[-1][1]
    assert 50 < f["opp_allow_rec_yds"] < 100                    # LV allowed 100 then 50 to WRs


def test_live_row_uses_injury_report():
    fb = F.FeatureBuilder()
    list(fb.walk([game_row(1, key="star", targets=10), game_row(1, key="me", targets=3)]))
    ctx = {"home": 0.0, "spread": -3.0, "implied_pts": 21.0, "game_total": 45.0}
    out = fb.live_row("me", "WR", "KC", "LV", 2025, 2, ctx, {("KC", "star"): "Out"})
    fine = fb.live_row("me", "WR", "KC", "LV", 2025, 2, ctx, {})
    assert out["vac_tgt"] > 0.7 and fine["vac_tgt"] == 0 and out["n_games"] == 1


# --------------------------------------------------------------------------- distributions

@pytest.mark.parametrize("disp", [None, 5.0])
def test_count_probabilities(disp):
    dist = {"disp": disp}
    over, under, push = M.probabilities("REC", 4.6, 4.5, dist)
    assert math.isclose(over + under + push, 1) and push == 0
    over2, under2, push2 = M.probabilities("REC", 4.6, 4.0, dist)
    assert push2 > 0.1 and math.isclose(over2 + under2 + push2, 1)
    assert M.probabilities("REC", 4.6, 6.5, dist)[0] < over < M.probabilities("REC", 4.6, 2.5, dist)[0]


def test_negative_binomial_matches_poisson_limit():
    assert math.isclose(M._nb_cdf(3, 2.5, 1e7), M._nb_cdf(3, 2.5, None), rel_tol=1e-4)


def test_dispersion_fit():
    rng = np.random.default_rng(0)
    mu = rng.uniform(2, 8, 20000)
    y = rng.negative_binomial(4.0, 4.0 / (4.0 + mu))
    assert 3.3 < M.fit_dispersion(mu, y) < 4.8
    disp = M.fit_dispersion(mu, rng.poisson(mu).astype(float))
    assert disp is None or disp > 50                           # Poisson data: no (or negligible) overdispersion


def test_yards_distribution_is_calibrated():
    rng = np.random.default_rng(1)
    mu = rng.uniform(20, 120, 40000)
    y = np.round(rng.gamma(2.0, mu / 2.0))
    dist = M.fit_ratio_bins(mu, y)
    # P(over the projection) of a gamma(2) is ~0.41 regardless of the mean
    for m in (30.0, 75.0, 110.0):
        over, under, push = M.probabilities("REC_YDS", m, m - 0.5 if m == int(m) else m, dist)
        assert 0.36 < over < 0.46 and math.isclose(over + under + push, 1)
    over, under, push = M.probabilities("REC_YDS", 60.0, 60.0, dist)
    assert 0 < push < 0.03


def test_anytime_td_platt():
    rng = np.random.default_rng(2)
    p = rng.uniform(0.05, 0.6, 30000)
    y = (rng.uniform(size=p.size) < p * 0.8).astype(float)      # the raw model overstates by 25%
    ab = M.fit_platt(p, y)
    dist = {"platt": ab}
    over, under, push = M.probabilities("ANYTIME_TD", 0.5, 0.5, dist)
    assert 0.36 < over < 0.44 and push == 0


def test_markets_match_odds_and_grading():
    from spm.odds import NFL_MARKETS
    from spm.store.grading import NFL_MARKETS as GRADED
    assert set(M.MARKETS) == set(NFL_MARKETS) == set(GRADED)
