"""NFL prop models: one LightGBM model per market, trained walk-forward.

  tune      train <= 2022, choose model size and rounds on 2023
  calibrate train <= 2023 -> predict 2024; with the 2023 tune predictions these fit each
            market's outcome distribution (never on seasons the model saw)
  holdout   train <= 2024 -> predict 2025, scored exactly once
  final     train on every completed game (through the current week) for live use

What each model predicts, and how P(over) is computed from it:
  counts (REC, PASS_ATT, PASS_TD, ...): the mean (Poisson objective); outcomes are negative
      binomial around it, with the dispersion fit out of sample
  yards (PASS_YDS, REC_YDS, ...): the mean (L2); outcomes follow the out-of-sample distribution
      of actual / projected, by projection size (captures skew and zero-yard games)
  ANYTIME_TD: the probability directly (binary), recalibrated (Platt) out of sample

Models and their metadata (features, distributions, scorecard, data provenance) are saved to
models/nfl/ in the repo, so the cloud scoring job can use them without retraining.
"""

import datetime as dt
import json
import math
import os

import lightgbm as lgb
import numpy as np

from . import data
from .features import FEATURES, GROUPS, value

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "models", "nfl")
TUNE, CALIB, HOLDOUT = 2023, 2024, 2025
BASE_PARAMS = {"learning_rate": 0.03, "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1,
               "lambda_l2": 2.0, "verbose": -1, "num_threads": 0, "seed": 11}
GRID = [{"num_leaves": 15, "min_data_in_leaf": 200}, {"num_leaves": 31, "min_data_in_leaf": 100},
        {"num_leaves": 63, "min_data_in_leaf": 60}]
MAX_ROUNDS = 3000
YARD_BINS = 8           # projection-size bins for the yards distributions


def _f(feats, k):
    v = feats.get(k)
    return 0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _qb(f):
    """The team's recent primary passer: who books post QB lines for (backups' relief games excluded)."""
    return f["pos_QB"] == 1.0 and _f(f, "pass_att_s") >= 15 and _f(f, "pass_share_s") >= 0.7


def _rusher(f):
    return _f(f, "rush_att_s") >= 4


def _receiver(f):
    return f["pos_QB"] == 0.0 and _f(f, "targets_s") >= 2.5


def _touches(f):
    return f["pos_QB"] == 0.0 and _f(f, "rush_att_s") + _f(f, "targets_s") >= 5


def _scorer(f):
    return _qb(f) or (f["pos_QB"] == 0.0 and _f(f, "rush_att_s") + _f(f, "targets_s") >= 2.5)


# market -> stat columns summed, kind, who's in the training set (pre-game role, never the outcome)
MARKETS = {
    "PASS_YDS": (("pass_yds",), "yards", _qb), "PASS_CMP": (("pass_cmp",), "count", _qb),
    "PASS_ATT": (("pass_att",), "count", _qb), "PASS_TD": (("pass_td",), "count", _qb),
    "INT": (("pass_int",), "count", _qb),
    "RUSH_YDS": (("rush_yds",), "yards", _rusher), "RUSH_ATT": (("rush_att",), "count", _rusher),
    "REC": (("rec",), "count", _receiver), "REC_YDS": (("rec_yds",), "yards", _receiver),
    "RUSH_REC_YDS": (("rush_yds", "rec_yds"), "yards", _touches),
    "ANYTIME_TD": (("any_td",), "binary", _scorer),
}
LABELS = {"PASS_YDS": "passing yards", "PASS_CMP": "completions", "PASS_ATT": "pass attempts",
          "PASS_TD": "passing TDs", "INT": "interceptions", "RUSH_YDS": "rushing yards",
          "RUSH_ATT": "rushing attempts", "REC": "receptions", "REC_YDS": "receiving yards",
          "RUSH_REC_YDS": "rushing + receiving yards", "ANYTIME_TD": "touchdowns"}
OBJECTIVE = {"count": "poisson", "yards": "regression", "binary": "binary"}


def label(row, market):
    cols = MARKETS[market][0]
    y = sum(value(row, c) for c in cols)
    return float(y >= 1) if MARKETS[market][1] == "binary" else float(y)


def baseline(f, market):
    """What a simple handicapper would project: season average after 3 games, else recent form."""
    cols = MARKETS[market][0]
    get = lambda suffix: sum(_f(f, f"{c}_{suffix}") for c in cols)
    if f["season_games"] >= 3:
        est = get("szn")
    elif not math.isnan(f.get(f"{cols[0]}_l", float("nan"))):
        est = get("l")
    else:
        est = get("prev")
    if MARKETS[market][1] == "binary":
        return min(max(1 - math.exp(-est), 0.02), 0.95)    # P(at least one) at the player's TD rate
    return est


# --------------------------------------------------------------------------- distributions

def _nb_cdf(k, mu, disp):
    """P(Y <= k) for negative binomial with mean mu and dispersion disp (None = Poisson)."""
    if k < 0:
        return 0.0
    mu = max(mu, 1e-6)
    if disp is None:
        p, term, total = math.exp(-mu), math.exp(-mu), math.exp(-mu)
        for i in range(1, int(k) + 1):
            term *= mu / i
            total += term
        return min(total, 1.0)
    r, q = disp, disp / (disp + mu)
    term = q ** r
    total = term
    for i in range(1, int(k) + 1):
        term *= (r + i - 1) / i * (1 - q)
        total += term
    return min(total, 1.0)


def fit_dispersion(mu, y):
    """Method of moments: Var = mu + mu^2 / disp. None when no overdispersion (Poisson)."""
    excess = float(np.sum((y - mu) ** 2 - mu))
    return float(np.sum(mu ** 2) / excess) if excess > 0 else None


def fit_ratio_bins(mu, y):
    """Yards: bins of projection size, each with sorted actual/projected ratios."""
    mu = np.maximum(mu, 1.0)
    edges = np.quantile(mu, np.linspace(0, 1, YARD_BINS + 1))[1:-1].tolist()
    idx = np.searchsorted(edges, mu)
    return {"edges": edges, "ratios": [np.sort(y[idx == b] / mu[idx == b]).round(4).tolist()
                                       for b in range(YARD_BINS)]}


def fit_platt(p, y):
    """Logistic recalibration a*logit(p)+b by Newton's method."""
    x = np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))
    a, b = 1.0, 0.0
    for _ in range(50):
        z = 1 / (1 + np.exp(-(a * x + b)))
        g = np.array([np.sum((z - y) * x), np.sum(z - y)])
        w = z * (1 - z)
        h = np.array([[np.sum(w * x * x), np.sum(w * x)], [np.sum(w * x), np.sum(w)]]) + 1e-6 * np.eye(2)
        a, b = np.array([a, b]) - np.linalg.solve(h, g)
    return [float(a), float(b)]


def _platt(p, ab):
    x = math.log(min(max(p, 1e-4), 1 - 1e-4) / (1 - min(max(p, 1e-4), 1 - 1e-4)))
    return 1 / (1 + math.exp(-(ab[0] * x + ab[1])))


def probabilities(market, mu, line, dist):
    """(p_over, p_under, p_push) for a line: the fitted distribution, then (if fitted) the
    out-of-sample recalibration of P(over), which corrects overconfidence at the extremes."""
    p_over, p_under, p_push = _raw_probabilities(market, mu, line, dist)
    if "platt_over" in dist and MARKETS[market][1] != "binary":
        live = 1 - p_push
        if live > 0:
            p_over = _platt(p_over / live, dist["platt_over"]) * live
            p_under = live - p_over
    return p_over, p_under, p_push


def _raw_probabilities(market, mu, line, dist):
    kind = MARKETS[market][1]
    if kind == "binary":
        p = _platt(mu, dist["platt"])
        if line < 1:                           # "anytime TD" = over 0.5
            return p, 1 - p, 0.0
        raise ValueError("binary markets only have line 0.5")
    if kind == "count":
        lo = math.floor(line - 1e-9) if line != int(line) else int(line) - 1
        p_under = _nb_cdf(lo, mu, dist["disp"])
        p_push = (_nb_cdf(int(line), mu, dist["disp"]) - p_under) if line == int(line) else 0.0
        return 1 - p_under - p_push, p_under, p_push
    mu = max(mu, 1.0)
    b = int(np.searchsorted(dist["edges"], mu))
    ratios = dist["ratios"][b]
    n = len(ratios)
    below = lambda x: int(np.searchsorted(ratios, x / mu))      # outcomes with actual < x
    if line == int(line):     # whole-number line: actual == line is a push (yards are whole numbers)
        over, push = n - below(line + 0.5), below(line + 0.5) - below(line - 0.5)
    else:
        over, push = n - below(line), 0
    p_over, p_push = (over + 0.5) / (n + 1), push / (n + 1)
    return p_over, max(0.0, 1 - p_over - p_push), p_push


# --------------------------------------------------------------------------- training

def _matrix(feats):
    return np.array([[f[k] for k in FEATURES] for f in feats], dtype=np.float32)


def _fit(X, y, kind, params, rounds=None, valid=None):
    p = {**BASE_PARAMS, **params, "objective": OBJECTIVE[kind]}
    dtrain = lgb.Dataset(X, y, free_raw_data=False)
    if valid is not None:
        dvalid = lgb.Dataset(valid[0], valid[1], reference=dtrain)
        return lgb.train(p, dtrain, MAX_ROUNDS, valid_sets=[dvalid],
                         callbacks=[lgb.early_stopping(100, verbose=False)])
    return lgb.train(p, dtrain, rounds)


def _loss(kind, y, pred):
    if kind == "binary":
        p = np.clip(pred, 1e-4, 1 - 1e-4)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    return float(np.mean(np.abs(y - pred)))


def market_sets(built, market):
    """Eligible rows for one market: (seasons, X, y, baseline, rows)."""
    elig = MARKETS[market][2]
    sel = [(r, f) for r, f in built if elig(f)]
    seasons = np.array([r["season"] for r, _ in sel])
    X = _matrix([f for _, f in sel])
    y = np.array([label(r, market) for r, _ in sel])
    base = np.array([baseline(f, market) for _, f in sel])
    return seasons, X, y, base, sel


def _scorecard(market, kind, y, pred, base, dist):
    """Held-out accuracy, plus calibration of P(over) against the baseline as a stand-in line."""
    out = {"n": int(len(y))}
    if kind == "binary":
        p = np.array([_platt(v, dist["platt"]) for v in pred])
        out.update({"logloss": _loss(kind, y, p), "logloss_baseline": _loss(kind, y, np.clip(base, 0.02, 0.95)),
                    "brier": float(np.mean((p - y) ** 2)), "brier_baseline": float(np.mean((base - y) ** 2))})
        probs, outcome = p, y
    else:
        out.update({"mae": _loss(kind, y, pred), "mae_baseline": _loss(kind, y, base),
                    "mean_actual": float(np.mean(y)), "mean_pred": float(np.mean(pred))})
        lines = np.floor(np.maximum(base, 0.5)) + 0.5          # baseline rounded to a .5 line
        probs = np.array([probabilities(market, m, ln, dist)[0] for m, ln in zip(pred, lines)])
        outcome = (y > lines).astype(float)
        out["brier_vs_baseline_line"] = float(np.mean((probs - outcome) ** 2))
    bins = []
    for lo in np.arange(0, 1, 0.1):
        m = (probs >= lo) & (probs < lo + 0.1)
        if m.sum() >= 30:
            bins.append([round(float(probs[m].mean()), 3), round(float(outcome[m].mean()), 3), int(m.sum())])
    out["calibration"] = bins          # [predicted, actual, n] per decile of predicted P(over)
    return out


def _fit_dist(kind, pred, y):
    if kind == "binary":
        return {"platt": fit_platt(pred, y)}
    if kind == "count":
        return {"disp": fit_dispersion(np.maximum(pred, 1e-3), y)}
    return fit_ratio_bins(pred, y)


def train(built, log=print, save=True):
    """Walk-forward training for every market. Returns the report (also saved to models/nfl/)."""
    last = max(r["gameday"] for r, _ in built)
    report = {"trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "data_through": last, "features": list(FEATURES), "provenance": data.PROVENANCE,
              "protocol": f"tune {TUNE}, distributions fit on {TUNE}-{CALIB} out-of-sample predictions, "
                          f"holdout {HOLDOUT} scored once, final model on everything through {last}",
              "markets": {}}
    if save:
        os.makedirs(MODEL_DIR, exist_ok=True)
    for market, (_, kind, _) in MARKETS.items():
        seasons, X, y, base, _ = market_sets(built, market)
        tr, tu = seasons < TUNE, seasons == TUNE
        best = None
        for params in GRID:
            b = _fit(X[tr], y[tr], kind, params, valid=(X[tu], y[tu]))
            loss = _loss(kind, y[tu], b.predict(X[tu], num_iteration=b.best_iteration))
            if best is None or loss < best[0]:
                best = (loss, params, b.best_iteration, b)
        _, params, rounds, tune_model = best
        rounds = max(rounds, 50)
        pred_tune = tune_model.predict(X[tu], num_iteration=rounds)
        ca = seasons == CALIB
        calib_model = _fit(X[seasons < CALIB], y[seasons < CALIB], kind, params, rounds)
        pred_cal = calib_model.predict(X[ca])
        oos_pred, oos_y = np.concatenate([pred_tune, pred_cal]), np.concatenate([y[tu], y[ca]])
        dist = _fit_dist(kind, oos_pred, oos_y)
        if kind != "binary":
            lines = np.floor(np.maximum(np.concatenate([base[tu], base[ca]]), 0.5)) + 0.5
            raw = np.array([_raw_probabilities(market, m, ln, dist)[0] for m, ln in zip(oos_pred, lines)])
            dist["platt_over"] = fit_platt(raw, (oos_y > lines).astype(float))
        ho = seasons == HOLDOUT
        hold_model = _fit(X[seasons < HOLDOUT], y[seasons < HOLDOUT], kind, params, rounds)
        card = _scorecard(market, kind, y[ho], hold_model.predict(X[ho]), base[ho], dist)
        final = _fit(X, y, kind, params, rounds)
        imp = final.feature_importance("gain")
        top = sorted(zip(FEATURES, imp.tolist()), key=lambda t: -t[1])[:12]
        report["markets"][market] = {"kind": kind, "params": params, "rounds": int(rounds), "n_train": int(len(y)),
                                     "dist": dist, "holdout": card,
                                     "top_features": [[k, round(v / imp.sum(), 4)] for k, v in top]}
        msg = (f"{market:13s} n={len(y):6d} rounds={rounds:4d} " +
               (f"logloss {card['logloss']:.4f} vs {card['logloss_baseline']:.4f}" if kind == "binary" else
                f"MAE {card['mae']:.2f} vs baseline {card['mae_baseline']:.2f}"))
        log(msg)
        if save:
            final.save_model(os.path.join(MODEL_DIR, f"{market}.txt"))
    if save:
        with open(os.path.join(MODEL_DIR, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    return report


# --------------------------------------------------------------------------- live

class Predictor:
    """Trained NFL models + distributions from models/nfl/."""

    def __init__(self, model_dir=MODEL_DIR):
        with open(os.path.join(model_dir, "meta.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        self.version = self.meta["trained_at"]
        self.models = {m: lgb.Booster(model_file=os.path.join(model_dir, f"{m}.txt")) for m in self.meta["markets"]}

    @staticmethod
    def available(model_dir=MODEL_DIR):
        return os.path.exists(os.path.join(model_dir, "meta.json"))

    def project(self, market, feats):
        """Projection and per-group effects (SHAP) for one player-game."""
        X = _matrix([feats])
        b, kind = self.models[market], MARKETS[market][1]
        mu = float(b.predict(X)[0])
        contrib = b.predict(X, pred_contrib=True)[0]
        groups = {}
        for g, names in GROUPS.items():
            groups[g] = float(sum(contrib[FEATURES.index(n)] for n in names if n in FEATURES))
        return {"market": market, "kind": kind, "mu": mu, "raw_base": float(contrib[-1]), "groups": groups,
                "eligible": bool(MARKETS[market][2](feats)), "baseline": baseline(feats, market)}

    def with_line(self, proj, line):
        p_over, p_under, p_push = probabilities(proj["market"], proj["mu"], line,
                                                self.meta["markets"][proj["market"]]["dist"])
        return {**proj, "line": line, "p_over": p_over, "p_under": p_under, "p_push": p_push}
