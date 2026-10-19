"""Gradient-boosted projection models (LightGBM), one per stat.

Walk-forward protocol (no season is ever predicted by a model that saw it):

  tune      train <= T-3, pick model size + rounds on season T-2   (2023-24)
  validate  train <= T-2, predict T-1                               (2024-25)
            -> also fits the probability calibration (Platt) per stat
  holdout   train <= T-1, predict T, scored exactly once            (2025-26)
  final     train on everything through T for live predictions

The model predicts the mean of each stat (Poisson objective). The outcome
distribution around it is negative binomial with the player's own spread,
exactly like the formula model.
"""

import json
import math
import os
import time

import lightgbm as lgb
import numpy as np

from . import model
from .backtest import apply_platt, fit_platt
from .data import DATA_DIR, season_label
from .features import FEATURE_LABELS, FEATURES, KEYS, STAT_FEATS
from .lineups import LINEUP_FEATS

MODEL_DIR = os.path.join(DATA_DIR, "models")
BASE_PARAMS = {
    "objective": "poisson", "learning_rate": 0.05, "feature_fraction": 0.8,
    "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "verbose": -1,
    "num_threads": 0, "seed": 7,
}
GRID = [
    {"num_leaves": 15, "min_data_in_leaf": 400},
    {"num_leaves": 31, "min_data_in_leaf": 200},
    {"num_leaves": 63, "min_data_in_leaf": 100},
]
EVAL_MIN_MINUTES = 20  # prop-relevant players for reported metrics

# Features grouped for explanations. Player-level groups set the baseline;
# situational groups are what the reasoning reports as adjustments.
GROUPS = {
    "Opponent defense": ["opp_ratio", "opp_pts_ratio"],
    "Shot-zone matchup": ["zone_fg_edge", "zone_freq_edge", "opp_3p_delta", "opp_3pa_ratio", "opp_rim_delta"],
    "Play-type matchup": ["pt_ppp_edge", "pt_freq_edge"],
    "Teammates out": ["tm_missing_min", "tm_missing_pts", "tm_missing_ast", "tm_missing_reb", "tm_top_out"],
    "Opponent lineup": ["opp_missing_min", "opp_missing_def", "opp_missing_stocks", "opp_missing_reb",
                        "opp_active_def"],
    "Rest & schedule": ["rest", "b2b", "missed_games", "days_into_season"],
    "Home/road": ["home"],
    "Role & experience": ["experience", "games_with_team", "prior_share", "n_hist", "szn_n"],
}
SITUATIONAL = list(GROUPS)


def _fit(X, y, params, rounds=None, valid=None):
    ds = lgb.Dataset(X, y, feature_name=FEATURES, free_raw_data=False)
    if valid is not None:
        vs = lgb.Dataset(valid[0], valid[1], reference=ds)
        return lgb.train({**BASE_PARAMS, **params}, ds, num_boost_round=3000, valid_sets=[vs],
                         callbacks=[lgb.early_stopping(100, verbose=False)])
    return lgb.train({**BASE_PARAMS, **params}, ds, num_boost_round=rounds)


def _matrix(frame, k, mask):
    return np.hstack([frame["shared"][mask], frame["stat"][k][mask]])


def _eval_mask(frame):
    sh = frame["shared"]
    i10, i40 = FEATURES.index("min_10"), FEATURES.index("min_40")
    min_mix = 0.6 * sh[:, i10] + 0.4 * sh[:, i40]
    szn_n = frame["stat"][KEYS[0]][:, STAT_FEATS.index("szn_n")]
    return (min_mix >= EVAL_MIN_MINUTES) & (szn_n >= 10)


def _probs(mean, disp, infl, line):
    out = np.empty(len(mean))
    for i, (m, d, f, l) in enumerate(zip(mean, disp, infl, line)):
        m = max(float(m), 0.05)
        out[i] = model.probabilities(m, max(m * d * f, m + 1e-6), l)[0]
    return out


def _season_metrics(frame, k, mask, pred, cal=None):
    y = frame["y"][k][mask]
    st = frame["stat"][k][mask]
    col = lambda name: st[:, STAT_FEATS.index(name)]
    szn_avg, disp = col("szn_avg"), col("disp_40")
    base = frame["base"][k][mask]
    line = np.floor(szn_avg) + 0.5
    hit = (y > line).astype(float)
    p_raw = _probs(pred, disp, frame["infl"][mask], line)
    res = {
        "n": int(mask.sum()),
        "mae_ml": float(np.mean(np.abs(pred - y))),
        "mae_formula": float(np.mean(np.abs(base - y))),
        "mae_season_avg": float(np.mean(np.abs(szn_avg - y))),
        "brier_raw": float(np.mean((p_raw - hit) ** 2)),
        "brier_base_rate": float(hit.mean() * (1 - hit.mean())),
    }
    if cal:
        p_cal = np.array([apply_platt(p, cal["a"], cal["b"]) for p in p_raw])
        res["brier_cal"] = float(np.mean((p_cal - hit) ** 2))
    return res, p_raw, hit


def train(store, log=print, progress=lambda f: None):
    frame = store.train
    seasons = frame["season"]
    last = int(seasons.max())                 # most recent completed season (holdout)
    tune_s, val_s = last - 2, last - 1
    emask = _eval_mask(frame)
    os.makedirs(MODEL_DIR, exist_ok=True)
    report = {"trained_at": time.time(), "features": FEATURES, "first_season": season_label(int(seasons.min())),
              "tune_season": season_label(tune_s), "validation_season": season_label(val_s),
              "holdout_season": season_label(last), "rows": int(len(seasons)), "stats": {}, "calibration": []}
    report["typical"] = typical_context(frame)
    cal_points = {"raw": [], "cal": []}
    steps = len(KEYS) * (len(GRID) + 3)
    done = 0
    for k in KEYS:
        y = frame["y"][k]
        t0 = time.time()
        # 1) tune on season T-2
        tr, va = seasons < tune_s, seasons == tune_s
        best = None
        for params in GRID:
            b = _fit(_matrix(frame, k, tr), y[tr], params, valid=(_matrix(frame, k, va), y[va]))
            score = b.best_score["valid_0"]["poisson"]
            if best is None or score < best[0]:
                best = (score, params, b.best_iteration)
            done += 1
            progress(done / steps)
        _, params, rounds = best
        rounds = max(50, int(rounds * 1.05))
        # 2) validate on T-1 and fit calibration
        tr, va = seasons < val_s, seasons == val_s
        b = _fit(_matrix(frame, k, tr), y[tr], params, rounds)
        m = va & emask
        val_metrics, p_raw, hit = _season_metrics(frame, k, m, b.predict(_matrix(frame, k, m)))
        cal = fit_platt(list(zip(p_raw, hit)))
        done += 1
        progress(done / steps)
        # 3) holdout T, scored once
        tr, ho = seasons < last, seasons == last
        b = _fit(_matrix(frame, k, tr), y[tr], params, rounds)
        m = ho & emask
        ho_metrics, p_raw, hit = _season_metrics(frame, k, m, b.predict(_matrix(frame, k, m)), cal)
        cal_points["raw"] += list(zip(p_raw, hit))
        cal_points["cal"] += [(apply_platt(p, cal["a"], cal["b"]), h) for p, h in zip(p_raw, hit)]
        done += 1
        progress(done / steps)
        # 4) final model on everything
        b = _fit(_matrix(frame, k, np.ones(len(y), bool)), y, params, rounds)
        b.save_model(os.path.join(MODEL_DIR, f"{k}.txt"))
        gain = b.feature_importance("gain")
        top = np.argsort(-gain)[:12]
        report["stats"][k] = {
            "params": params, "rounds": rounds, "calibration": cal,
            "validation": val_metrics, "holdout": ho_metrics,
            "importance": [{"feature": FEATURES[i], "label": FEATURE_LABELS.get(FEATURES[i], FEATURES[i]),
                            "share": float(gain[i] / gain.sum())} for i in top],
        }
        done += 1
        progress(done / steps)
        log(f"  {k}: leaves={params['num_leaves']} rounds={rounds}  holdout MAE ml {ho_metrics['mae_ml']:.3f} "
            f"vs formula {ho_metrics['mae_formula']:.3f} vs season avg {ho_metrics['mae_season_avg']:.3f}  "
            f"({time.time() - t0:.0f}s)")

    report["calibration"] = {"raw": _bins(cal_points["raw"]), "calibrated": _bins(cal_points["cal"])}
    with open(os.path.join(MODEL_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f)
    return report


def typical_context(frame):
    """League-median context for an ordinary mid-season game (most recent season, games 20-60)."""
    sh = frame["shared"]
    szn_n = frame["stat"][KEYS[0]][:, STAT_FEATS.index("szn_n")]
    m = (frame["season"] == frame["season"].max()) & (szn_n >= 20) & (szn_n <= 60)
    out = {f: float(np.nanmedian(sh[m, FEATURES.index(f)])) for f in LINEUP_FEATS + ["prior_share"]}
    # medians of "share missing" are often 0; the mean reflects a typical night's absences better
    for f in ("tm_missing_min", "tm_missing_pts", "tm_missing_ast", "tm_missing_reb", "tm_top_out",
              "opp_missing_min", "opp_missing_def", "opp_missing_stocks", "opp_missing_reb"):
        out[f] = float(np.nanmean(sh[m, FEATURES.index(f)]))
    return out


def _bins(points):
    out = []
    for lo in range(0, 100, 10):
        sel = [(p, h) for p, h in points if lo / 100 <= p < (lo + 10) / 100]
        if len(sel) >= 30:
            out.append({"bin": f"{lo}-{lo + 10}%", "n": len(sel),
                        "predicted": sum(p for p, _ in sel) / len(sel),
                        "actual": sum(h for _, h in sel) / len(sel)})
    return out


# --------------------------------------------------------------------------- live predictor

class Predictor:
    def __init__(self):
        path = os.path.join(MODEL_DIR, "report.json")
        if not os.path.exists(path):
            raise FileNotFoundError("no trained model")
        with open(path, encoding="utf-8") as f:
            self.report = json.load(f)
        if self.report["features"] != FEATURES:
            raise ValueError("model was trained with a different feature set; retrain")
        self.boosters = {k: lgb.Booster(model_file=os.path.join(MODEL_DIR, f"{k}.txt")) for k in KEYS}

    def calibration(self, k):
        return self.report["stats"][k]["calibration"]

    def predict(self, k, X):
        return self.boosters[k].predict(np.atleast_2d(X))

    def drivers(self, k, X):
        """Situational adjustments as % effects on the projection (from SHAP values)."""
        contrib = self.boosters[k].predict(np.atleast_2d(X), pred_contrib=True)[0]
        out = []
        for g in SITUATIONAL:
            c = sum(contrib[FEATURES.index(f)] for f in GROUPS[g])
            out.append({"group": g, "effect": math.exp(c) - 1})
        return out


def load_predictor(log=print):
    try:
        return Predictor()
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"ML model not loaded ({e}); using formula model")
        return None
