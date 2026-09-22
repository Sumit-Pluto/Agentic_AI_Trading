"""quant/pipeline/pipeline.py — the orchestrator that wires all layers together.

build_training_set : strategy plugs -> enrich -> label   (the labeled dataset)
train              : fit meta-model + holdout metrics + walk-forward gate
infer              : triggers -> enrich -> predict -> filter stack -> select
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, brier_score_loss, accuracy_score

from .contracts import Candidate
from . import strategies, features as F, model as M
from .labeler import label_event
from .filters import FilterStack
from .selector import select


def build_training_set(dd, plugs=("grid", "orb", "pullback"), cost_pct=0.05,
                       stride=5, progress=None):
    bank = F.FeatureBank(dd)
    events = strategies.all_triggers(dd, bank, plugs=plugs, stride=stride)
    cands = []
    for i, ev in enumerate(events):
        lab = label_event(ev, dd, bank, cost_pct=cost_pct)
        if lab is None:
            continue
        label, pnl, reason = lab
        feats = F.enrich(ev, bank, dd)
        cands.append(Candidate(event=ev, features=feats, label=label,
                               outcome_pnl_pct=pnl, exit_reason=reason))
        if progress and i % 5000 == 0:
            progress(f"  labeled {i}/{len(events)}")
    cands.sort(key=lambda c: c.event.bar_time)
    return cands, bank


def train(dd, plugs=("grid", "orb", "pullback"), cost_pct=0.05, stride=5,
          progress=print):
    cands, bank = build_training_set(dd, plugs, cost_pct, stride, progress)
    X = M.build_matrix(cands)
    y = np.array([c.label for c in cands])
    times = pd.Series([pd.Timestamp(c.event.bar_time) for c in cands])
    n = len(cands)
    # time-ordered holdout
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
        "plugs": list(plugs), "strategies_seen": sorted({c.event.strategy_id for c in cands}),
    }
    return {"model": mdl, "bank": bank, "candidates": cands, "report": report}


def infer(dd, mdl, bank, filter_config=None, day=None, p_star=0.55, top_k=5,
          plugs=("grid", "orb", "pullback"), stride=5):
    events = strategies.all_triggers(dd, bank, plugs=plugs, stride=stride)
    if day:
        events = [e for e in events if e.bar_time[:10] == day]
    cands = [Candidate(event=e, features=F.enrich(e, bank, dd)) for e in events]
    if not cands:
        return {"chosen": [], "n_candidates": 0, "n_vetoed": 0,
                "filters": [], "day": day}
    X = M.build_matrix(cands)
    p = mdl.predict(X)
    for c, pi in zip(cands, p):
        c.p_win = float(pi)
    stack = FilterStack(filter_config)
    logs = [stack.apply(c) for c in cands]
    chosen = select(cands, p_star=p_star, top_k=top_k)
    return {"chosen": chosen, "n_candidates": len(cands),
            "n_vetoed": sum(1 for c in cands if not c.alive),
            "filters_enabled": stack.enabled_ids, "sample_log": logs[0] if logs else [],
            "day": day}
