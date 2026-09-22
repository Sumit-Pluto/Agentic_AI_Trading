"""quant/pipeline/ptrain.py — parallel training-set build (multiprocess over symbols).

The serial `pipeline.build_training_set` labels+enriches every trigger in one Python
loop on ONE core. Per-symbol work is fully independent (the only cross-symbol input is
the shared index return series, which we pass to every worker), so we fan out one task
per symbol across all cores, then fit the SAME LightGBM meta-model + walk-forward.

Correctness: features are per-symbol (rel_strength/beta use the shared index; liq_tier,
era, ban, vix, days_to_event are precomputed into `meta` by the loader), so a symbol's
rows are identical whether built alone or in the serial loop. The parent concatenates
all shards, time-sorts globally, and trains exactly as `pipeline.train` does.

Lives in its own module so the worker functions pickle by a stable qualified name
(`quant.pipeline.ptrain._build_one`) regardless of how the entry point is launched.
"""
from __future__ import annotations

import os
import time
import concurrent.futures as cf
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, accuracy_score, brier_score_loss

from .synthdata import DummyData
from . import strategies, features as F, model as M
from .labeler import label_event
from .features import NUMERIC, CATEGORICAL

# ── worker side ───────────────────────────────────────────────────────────────
_G = {}


def _winit(index, vix, plugs, cost_pct, stride):
    """Pool initializer: stash the shared, read-only inputs once per worker."""
    _G.update(index=index, vix=vix, plugs=plugs, cost_pct=float(cost_pct), stride=int(stride))


def _build_one(payload):
    """Build labeled+enriched feature rows for ONE symbol. Returns a DataFrame."""
    sym, candles_sym, chain_sym, meta_sym, days = payload
    dd = DummyData(symbols=[sym], candles={sym: candles_sym}, index=_G["index"],
                   vix=_G["vix"], chain=chain_sym, meta=meta_sym, days=days)
    bank = F.FeatureBank(dd)
    events = strategies.all_triggers(dd, bank, plugs=_G["plugs"], stride=_G["stride"])
    rows = []
    enrich = F.enrich
    for ev in events:
        lab = label_event(ev, dd, bank, cost_pct=_G["cost_pct"])
        if lab is None:
            continue
        feats = enrich(ev, bank, dd)
        feats["_label"] = lab[0]
        feats["_bar_time"] = ev.bar_time
        rows.append(feats)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── parent side ───────────────────────────────────────────────────────────────
def _payloads(dd):
    """Per-symbol slices of dd (chain/meta grouped once; index+vix passed via initializer)."""
    chain_by = defaultdict(dict)
    for key, v in dd.chain.items():
        chain_by[key[0]][key] = v
    meta_by = defaultdict(dict)
    for key, v in dd.meta.items():
        meta_by[key[0]][key] = v
    return [(s, dd.candles[s], chain_by.get(s, {}), meta_by.get(s, {}), dd.days)
            for s in dd.candles]


def train_parallel(dd, plugs=("grid", "orb", "pullback"), cost_pct=0.05, stride=5,
                   n_workers=None, progress=print):
    n_workers = n_workers or max(1, (os.cpu_count() or 2) - 1)
    payloads = _payloads(dd)
    if progress:
        progress(f"[parallel] {len(payloads)} symbols over {n_workers} workers")

    frames, t0 = [], time.time()
    with cf.ProcessPoolExecutor(max_workers=n_workers, initializer=_winit,
                                initargs=(dd.index, dd.vix, plugs, cost_pct, stride)) as ex:
        for i, df in enumerate(ex.map(_build_one, payloads)):
            if len(df):
                frames.append(df)
            if progress and (i % 20 == 0 or i == len(payloads) - 1):
                progress(f"  built {i+1}/{len(payloads)} symbols, "
                         f"{sum(len(f) for f in frames)} samples, {time.time()-t0:.0f}s")
    if not frames:
        raise RuntimeError("parallel build produced no samples")

    full = (pd.concat(frames, ignore_index=True)
            .sort_values("_bar_time", kind="stable").reset_index(drop=True))

    # assemble the model matrix exactly like model.build_matrix
    for col in NUMERIC:
        if col not in full:
            full[col] = 0.0
    for col in CATEGORICAL:
        if col not in full:
            full[col] = "na"
        full[col] = full[col].astype("category")
    X = full[NUMERIC + CATEGORICAL]
    y = full["_label"].to_numpy()
    times = pd.Series(pd.to_datetime(full["_bar_time"]))

    # fit + holdout + walk-forward (same recipe as pipeline.train)
    n = len(full)
    cut = int(n * 0.8)
    mdl = M.MetaModel().fit(X.iloc[:cut], y[:cut])
    p_test = mdl.predict(X.iloc[cut:])
    y_test = y[cut:]
    auc = roc_auc_score(y_test, p_test) if len(np.unique(y_test)) > 1 else float("nan")
    acc = accuracy_score(y_test, (p_test >= 0.5).astype(int))
    brier = brier_score_loss(y_test, p_test) if len(np.unique(y_test)) > 1 else None
    wf = M.walk_forward(X, y, times)
    report = {
        "samples": n, "features": X.shape[1], "base_rate": round(float(y.mean()), 4),
        "holdout": {"n": len(y_test), "auc": round(float(auc), 4),
                    "accuracy": round(float(acc), 4),
                    "brier": round(float(brier), 4) if brier is not None else None},
        "walk_forward": wf,
        "top_features": dict(list(mdl.importance().items())[:12]),
        "plugs": list(plugs),
        "strategies_seen": sorted(full["strategy_id"].astype(str).unique().tolist())
        if "strategy_id" in full else [],
        "n_workers": n_workers,
    }
    return {"model": mdl, "report": report}
