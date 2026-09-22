"""quant/pipeline/model.py — LAYER D meta-model + walk-forward validation.

LightGBM over the enriched feature vector -> P(win), then isotonic calibration on a
held-out slice (so the probability is trustworthy for sizing). Walk-forward (purged,
time-ordered) gives the honest out-of-sample AUC used as the ship gate. Categoricals
(strategy_id, setup_id, era, liq_tier) go in as native LightGBM categories, so
per-strategy / per-regime behaviour is learned from splits — never separate models.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, brier_score_loss

from .features import NUMERIC, CATEGORICAL

LGB_PARAMS = dict(objective="binary", n_estimators=400, learning_rate=0.05,
                  num_leaves=31, min_child_samples=60, subsample=0.8,
                  colsample_bytree=0.8, reg_lambda=1.0, verbosity=-1)


def build_matrix(candidates) -> pd.DataFrame:
    rows = [c.features for c in candidates]
    X = pd.DataFrame(rows)
    for col in NUMERIC:
        if col not in X:
            X[col] = 0.0
    for col in CATEGORICAL:
        if col not in X:
            X[col] = "na"
        X[col] = X[col].astype("category")
    return X[NUMERIC + CATEGORICAL]


def _auc(y, p):
    return roc_auc_score(y, p) if len(np.unique(y)) > 1 else float("nan")


class MetaModel:
    def __init__(self, params=None):
        self.params = params or LGB_PARAMS
        self.model = None
        self.iso = None
        self.cat_dtypes = {}

    def _prep(self, X):
        X = X.copy()
        for col in CATEGORICAL:
            if col in self.cat_dtypes:
                X[col] = X[col].astype(self.cat_dtypes[col])
            else:
                X[col] = X[col].astype("category")
        return X

    def fit(self, X, y):
        n = len(X)
        cut = int(n * 0.85)                      # last 15% (time-ordered) = calib/valid
        Xtr, ytr = X.iloc[:cut], y[:cut]
        Xva, yva = X.iloc[cut:], y[cut:]
        for col in CATEGORICAL:
            self.cat_dtypes[col] = X[col].astype("category").dtype
        Xtr, Xva = self._prep(Xtr), self._prep(Xva)
        self.model = lgb.LGBMClassifier(**self.params)
        self.model.fit(Xtr, ytr, eval_set=[(Xva, yva)],
                       callbacks=[lgb.early_stopping(40, verbose=False)],
                       categorical_feature=CATEGORICAL)
        raw_va = self.model.predict_proba(Xva)[:, 1]
        self.iso = IsotonicRegression(out_of_bounds="clip")
        if len(np.unique(yva)) > 1:
            self.iso.fit(raw_va, yva)
        return self

    def predict(self, X):
        raw = self.model.predict_proba(self._prep(X))[:, 1]
        if self.iso is not None and hasattr(self.iso, "X_min_"):
            return np.clip(self.iso.predict(raw), 0.001, 0.999)
        return raw

    def importance(self):
        if self.model is None:
            return {}
        imp = self.model.feature_importances_
        names = NUMERIC + CATEGORICAL
        return dict(sorted(zip(names, imp), key=lambda kv: -kv[1]))


def walk_forward(X, y, times, n_splits=5, params=None):
    """Time-ordered expanding-window OOS AUC (purged by 1 day). Returns dict."""
    params = params or LGB_PARAMS
    order = np.argsort(times.values if hasattr(times, "values") else times)
    X, y = X.iloc[order].reset_index(drop=True), np.asarray(y)[order]
    days = pd.Series(pd.to_datetime(pd.Series(times).values[order])).dt.normalize()
    n = len(X)
    bounds = [int(round(n * k / (n_splits + 1))) for k in range(1, n_splits + 2)]
    oos_p, oos_y, folds = [], [], 0
    for k in range(len(bounds) - 1):
        tr_end, te_end = bounds[k], bounds[k + 1]
        if tr_end < 50 or te_end <= tr_end:
            continue
        cutoff = days.iloc[tr_end] - pd.Timedelta(days=1)      # 1-day embargo
        mask = (np.arange(n) < tr_end) & (days.values <= cutoff.to_datetime64())
        ytr = y[mask]
        if ytr.sum() < 10 or (len(ytr) - ytr.sum()) < 10:
            continue
        m = lgb.LGBMClassifier(**params)
        Xtr = X[mask].copy(); Xte = X.iloc[tr_end:te_end].copy()
        for col in CATEGORICAL:
            Xtr[col] = Xtr[col].astype("category"); Xte[col] = Xte[col].astype("category")
        m.fit(Xtr, ytr, categorical_feature=CATEGORICAL)
        oos_p.append(m.predict_proba(Xte)[:, 1])
        oos_y.append(y[tr_end:te_end]); folds += 1
    if folds == 0:
        return {"folds": 0, "oos_auc": None, "beats": False, "n_oos": 0}
    yy, pp = np.concatenate(oos_y), np.concatenate(oos_p)
    auc = _auc(yy, pp)
    return {"folds": folds, "n_oos": int(len(yy)), "oos_auc": round(float(auc), 4),
            "oos_brier": round(float(brier_score_loss(yy, pp)), 4) if len(np.unique(yy)) > 1 else None,
            "base_rate": round(float(yy.mean()), 4),
            "beats": bool(auc is not None and auc > 0.55)}
