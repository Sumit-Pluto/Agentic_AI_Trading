"""quant/training/validate.py — walk-forward validation + baseline gate.

The design doc (docs/AGENT_TRAINING_AND_SCALING.md §3) makes these rules
NON-NEGOTIABLE, because in-sample fit on a time series is how backtests lie:

  * walk-forward only — train on the past, test on the *next* block, roll
    forward; never shuffle time;
  * purge/embargo — drop training rows whose day is within `embargo_days` of the
    test block (our trades resolve intraday, so 1 day is enough);
  * baseline gate — trained weights ship ONLY if they beat the current config
    out-of-sample by a margin; otherwise keep the hand-tuned defaults.

We pool the out-of-sample predictions across folds and compare two scorers on
them: the trained logistic model vs. the CURRENT config weights (a weighted
average of the family scores — exactly what the live tree computes). Higher
pooled OOS AUC wins.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .collector import FAMILIES
from .dataset import DirData
from .trainer import _fit_logistic, _sigmoid, _auc


def _baseline_composite(raw: np.ndarray, weights: dict) -> np.ndarray:
    """Composite score under a weight dict — the live tree's weighted average
    over family scores. Higher = more bullish-for-the-direction, so it doubles
    as a win-probability ranker for AUC."""
    w = np.array([max(float(weights.get(k, 1.0)), 0.0) for k in FAMILIES])
    s = w.sum() or 1.0
    return (raw * w).sum(axis=1) / s


def walk_forward(d: DirData, defaults: dict, *, n_splits: int = 5,
                 embargo_days: int = 1, l2: float = 1.0,
                 margin: float = 0.02) -> dict:
    """Expanding-window walk-forward. Returns pooled OOS metrics for the trained
    model and the baseline, plus ``beats_baseline``. Fewer folds run if the data
    is too small to make `n_splits` honest folds."""
    n = d.n
    if n < 2 * n_splits:
        n_splits = max(2, n // 10)                 # not enough for 5 clean folds

    # fold boundaries over the time-ordered rows; first block is train-only
    bounds = [int(round(n * k / (n_splits + 1))) for k in range(1, n_splits + 2)]
    dates = pd.to_datetime(d.dates)

    oos_model, oos_base, oos_y = [], [], []
    folds_used = 0
    for k in range(len(bounds) - 1):
        tr_end, te_end = bounds[k], bounds[k + 1]
        if tr_end < 10 or te_end <= tr_end:
            continue
        test_start_day = dates[tr_end]
        # embargo: drop trailing train rows within embargo_days of the test day
        cutoff = test_start_day - pd.Timedelta(days=embargo_days)
        tr_mask = np.zeros(n, dtype=bool)
        tr_mask[:tr_end] = True
        tr_mask &= np.asarray(dates <= cutoff)
        Xtr, ytr = d.X[tr_mask], d.y[tr_mask]
        if ytr.sum() < 3 or (len(ytr) - ytr.sum()) < 3:
            continue                               # need both classes to fit
        Xte = d.X[tr_end:te_end]
        yte = d.y[tr_end:te_end]
        raw_te = d.raw[tr_end:te_end]
        if len(yte) == 0:
            continue

        intercept, beta = _fit_logistic(Xtr, ytr, l2=l2)
        p_model = _sigmoid(intercept + Xte @ beta)
        p_base = _baseline_composite(raw_te, defaults)

        oos_model.append(p_model)
        oos_base.append(p_base)
        oos_y.append(yte)
        folds_used += 1

    if folds_used == 0 or not oos_y:
        return {"folds": 0, "n_oos": 0, "model_auc": None,
                "baseline_auc": None, "beats_baseline": False,
                "reason": "not enough data for an honest walk-forward"}

    y = np.concatenate(oos_y)
    pm = np.concatenate(oos_model)
    pb = np.concatenate(oos_base)
    model_auc = _auc(y, pm)
    base_auc = _auc(y, pb)
    beats = (model_auc is not None and base_auc is not None
             and model_auc >= base_auc + margin and model_auc > 0.5)
    return {"folds": folds_used, "n_oos": int(len(y)),
            "model_auc": (round(model_auc, 4) if model_auc is not None else None),
            "baseline_auc": (round(base_auc, 4) if base_auc is not None else None),
            "oos_win_rate": round(float(y.mean()), 4),
            "beats_baseline": bool(beats),
            "margin": margin}
