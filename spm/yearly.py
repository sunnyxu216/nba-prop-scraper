"""Year-by-year walk-forward test and feature-group ablation.

For every season S from 2018-19 on, train only on seasons before S (with the
model sizes chosen during training) and predict all of S. This shows how the
model would have done each year had it been used then, not just on one holdout.

The ablation retrains the most recent season with each feature group removed,
to measure what that data actually adds.
"""

import json
import os
import time

import lightgbm as lgb
import numpy as np

from . import ml
from .data import season_label
from .features import FEATURES, PT_FEATS, STAT_FEATS, ZONE_FEATS
from .lineups import LINEUP_FEATS

STATS = ("PTS", "REB", "AST", "3PM", "PRA")
FIRST_TEST = 2018
PATH = os.path.join(ml.MODEL_DIR, "yearly.json")
ABLATIONS = {
    "Lineups (who's playing)": LINEUP_FEATS,
    "Shot zones + play types": ZONE_FEATS + PT_FEATS,
}


def _fit_predict(frame, k, train_mask, test_mask, params, rounds, cols=None):
    X = ml._matrix(frame, k, train_mask)
    Xt = ml._matrix(frame, k, test_mask)
    if cols is not None:
        X, Xt = X[:, cols], Xt[:, cols]
    b = lgb.train({**ml.BASE_PARAMS, **params}, lgb.Dataset(X, frame["y"][k][train_mask]), num_boost_round=rounds)
    return b.predict(Xt)


def _metrics(frame, k, mask, pred):
    y = frame["y"][k][mask]
    st = frame["stat"][k][mask]
    szn = st[:, STAT_FEATS.index("szn_avg")]
    base = frame["base"][k][mask]
    line = np.floor(szn) + 0.5
    hit = y > line
    # did the model lean the right way against a line at the season-to-date average?
    lean_over = pred > line
    return {
        "n": int(mask.sum()),
        "mae_ml": float(np.mean(np.abs(pred - y))),
        "mae_formula": float(np.mean(np.abs(base - y))),
        "mae_season_avg": float(np.mean(np.abs(szn - y))),
        "side_accuracy": float(np.mean(lean_over == hit)),
    }


def run(store, log=print, progress=lambda f: None):
    with open(os.path.join(ml.MODEL_DIR, "report.json"), encoding="utf-8") as f:
        report = json.load(f)
    frame = store.train
    seasons = frame["season"]
    last = int(seasons.max())
    emask = ml._eval_mask(frame)
    tests = list(range(FIRST_TEST, last + 1))
    steps = len(STATS) * (len(tests) + len(ABLATIONS) + 1)
    done = 0
    out = {"generated": time.time(), "stats": list(STATS), "seasons": [], "ablation": {}}

    for s in tests:
        row = {"season": season_label(s), "stats": {}}
        for k in STATS:
            st = report["stats"][k]
            m = (seasons == s) & emask
            pred = _fit_predict(frame, k, seasons < s, m, st["params"], st["rounds"])
            row["stats"][k] = _metrics(frame, k, m, pred)
            done += 1
            progress(done / steps)
        out["seasons"].append(row)
        log(f"  {row['season']}: " + "  ".join(
            f"{k} {v['mae_ml']:.2f} vs {v['mae_season_avg']:.2f}" for k, v in row["stats"].items()))

    m = (seasons == last) & emask
    for k in STATS:
        st = report["stats"][k]
        full = _fit_predict(frame, k, seasons < last, m, st["params"], st["rounds"])
        full_mae = float(np.mean(np.abs(full - frame["y"][k][m])))
        done += 1
        progress(done / steps)
        for name, feats in ABLATIONS.items():
            cols = [i for i, f in enumerate(FEATURES) if f not in feats]
            pred = _fit_predict(frame, k, seasons < last, m, st["params"], st["rounds"], cols)
            mae = float(np.mean(np.abs(pred - frame["y"][k][m])))
            out["ablation"].setdefault(name, {})[k] = {"with": full_mae, "without": mae,
                                                      "improvement": (mae - full_mae) / mae}
            done += 1
            progress(done / steps)
    out["ablation_season"] = season_label(last)
    for name, res in out["ablation"].items():
        log(f"  {name}: " + "  ".join(f"{k} {v['improvement']:+.2%}" for k, v in res.items()))

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def load():
    if not os.path.exists(PATH):
        return None
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)
