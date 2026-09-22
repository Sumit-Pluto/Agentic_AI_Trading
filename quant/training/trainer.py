"""quant/training/trainer.py — learn per-direction family weights.

Objective (chosen): WIN PROBABILITY. A pure-numpy L2-regularized logistic
regression predicts win (1) / loss (0) from the 5 family scores, separately for
BUY and SELL. The fitted coefficients become the live tree's weights:

    weight_i = clip(beta_i, floor .. inf)      # a family that predicts LOSSES
                                               # (beta<=0) is down-weighted to
                                               # the floor, never made negative
    weights  = weights * (1 / mean(weights))   # renormalized to mean 1.0 so the
                                               # composite scale — and therefore
                                               # the signal_threshold — keeps its
                                               # current meaning

No sklearn: Newton/IRLS on 6 parameters (intercept + 5 families) with a ridge
term for conditioning. Deliberately conservative — it REFUSES to emit weights
when the evidence is too thin (min samples, both classes present), because
overfitting 30 trades into confident weights is worse than shipping the
untrained 1.0 defaults.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .collector import FAMILIES
from .dataset import DirData

# ── guardrails: below these, we report but do NOT emit weights ───────────────
MIN_SAMPLES = 200          # per direction
MIN_PER_CLASS = 30         # need this many wins AND this many losses
MIN_AVAIL = 0.05           # a family present in <5% of rows keeps its default
WEIGHT_FLOOR = 0.1         # trained weights never fall below this (stay in mix)


@dataclass
class DirResult:
    direction: str
    trained: bool                                      # in-sample fit succeeded
    reason: str = ""
    n: int = 0
    win_rate: float = 0.0
    weights: dict = field(default_factory=dict)        # family -> trained weight
    prev_weights: dict = field(default_factory=dict)   # family -> weight before
    coefs: dict = field(default_factory=dict)          # family -> raw beta
    intercept: float = 0.0
    availability: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)        # in-sample acc/auc/logloss
    oos: dict = field(default_factory=dict)            # walk-forward OOS metrics
    beats_baseline: bool = False                       # OOS gate vs current cfg

    @property
    def ship(self) -> bool:
        """Only weights that fit AND beat the current config out-of-sample may
        be written — the design doc's baseline gate."""
        return bool(self.trained and self.beats_baseline)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def _fit_logistic(X, y, l2=1.0, iters=100, tol=1e-8, prior=None):
    """IRLS (Newton) with a ridge on non-intercept terms. X is (n, k); returns
    (beta_intercept, beta_features[k]).

    ``prior`` (length-k vector) shrinks the coefficients toward it instead of
    toward zero — the partial-pooling knob for per-stock fits: pass the global
    fit's coefficients so a data-thin stock stays near the shared behaviour and
    a data-rich one is free to diverge."""
    n, k = X.shape
    Xa = np.hstack([np.ones((n, 1)), X])          # augment with intercept
    prior_aug = np.zeros(k + 1)
    if prior is not None:
        prior_aug[1:] = np.asarray(prior, dtype=float)
    beta = prior_aug.copy()                        # warm-start at the prior
    reg = np.eye(k + 1) * l2
    reg[0, 0] = 0.0                                # don't penalize the intercept
    for _ in range(iters):
        p = _sigmoid(Xa @ beta)
        W = np.clip(p * (1 - p), 1e-6, None)
        grad = Xa.T @ (p - y) + reg @ (beta - prior_aug)
        H = (Xa * W[:, None]).T @ Xa + reg + np.eye(k + 1) * 1e-8
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(H, grad, rcond=None)[0]
        beta -= step
        if np.max(np.abs(step)) < tol:
            break
    return float(beta[0]), beta[1:]


def _auc(y, p):
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    return float((ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2.0)
                 / (len(pos) * len(neg)))


def _metrics(y, p):
    eps = 1e-12
    pred = (p >= 0.5).astype(float)
    acc = float((pred == y).mean())
    ll = float(-np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))
    return {"accuracy": round(acc, 4), "auc": (round(_auc(y, p), 4)
            if _auc(y, p) is not None else None),
            "log_loss": round(ll, 4), "base_rate": round(float(y.mean()), 4)}


def _coefs_to_weights(coefs, availability, defaults, floor):
    """Map logistic coefficients -> non-negative, mean-1.0 family weights.

    A family we couldn't learn (too rarely available) keeps its default weight;
    the rest are clipped at the floor and renormalized to mean 1.0."""
    learned_keys, learned_vals = [], []
    weights = {}
    for j, key in enumerate(FAMILIES):
        if availability.get(key, 0.0) < MIN_AVAIL:
            weights[key] = defaults.get(key, 1.0)          # can't learn it
        else:
            w = max(float(coefs[j]), floor)
            weights[key] = w
            learned_keys.append(key)
            learned_vals.append(w)
    if learned_vals:                                       # renormalize learned
        mean_w = float(np.mean(learned_vals)) or 1.0
        for key in learned_keys:
            weights[key] = round(weights[key] / mean_w, 3)
    return weights


def train_direction(d: DirData, defaults: dict, *, l2: float = 1.0,
                    min_samples: int = MIN_SAMPLES,
                    min_per_class: int = MIN_PER_CLASS,
                    floor: float = WEIGHT_FLOOR) -> DirResult:
    n = d.n
    wins = int(d.y.sum())
    losses = n - wins
    res = DirResult(direction=d.direction, trained=False, n=n,
                    win_rate=round(d.win_rate, 4), availability=d.availability,
                    prev_weights={k: defaults.get(k, 1.0) for k in FAMILIES})
    if n < min_samples:
        res.reason = (f"only {n} labeled samples (need >= {min_samples}) — "
                      "keeping current weights; collect more first")
        return res
    if wins < min_per_class or losses < min_per_class:
        res.reason = (f"class imbalance: {wins} wins / {losses} losses "
                      f"(need >= {min_per_class} of each)")
        return res

    intercept, beta = _fit_logistic(d.X, d.y, l2=l2)
    p = _sigmoid(np.hstack([np.ones((n, 1)), d.X]) @ np.concatenate(
        [[intercept], beta]))
    res.intercept = round(intercept, 4)
    res.coefs = {FAMILIES[j]: round(float(beta[j]), 4)
                 for j in range(len(FAMILIES))}
    res.metrics = _metrics(d.y, p)
    res.weights = _coefs_to_weights(beta, d.availability, defaults, floor)
    res.trained = True

    # walk-forward + baseline gate (imported lazily — validate imports us)
    from .validate import walk_forward
    res.oos = walk_forward(d, defaults, l2=l2)
    res.beats_baseline = bool(res.oos.get("beats_baseline"))
    if res.ship:
        res.reason = "ok — beats current config out-of-sample"
    else:
        res.reason = ("fit ok but does NOT beat current config out-of-sample "
                      f"(model auc={res.oos.get('model_auc')} vs baseline "
                      f"{res.oos.get('baseline_auc')}) — keeping defaults")
    return res


def train(data: dict[str, DirData], defaults: dict | None = None,
          **kw) -> dict[str, DirResult]:
    """Train both directions. ``defaults`` maps family -> current weight for
    that direction's fallback (pass the tree defaults; None = 1.0 everywhere)."""
    defaults = defaults or {}
    return {direction: train_direction(
                data[direction], defaults.get(direction, {}), **kw)
            for direction in ("BUY", "SELL")}


def train_per_symbol(global_data: dict, per_sym_data: dict, defaults: dict,
                     *, l2: float = 1.0, min_samples: int = MIN_SAMPLES,
                     min_per_class: int = MIN_PER_CLASS,
                     floor: float = WEIGHT_FLOOR) -> dict:
    """Per-stock weights with partial pooling.

    global_data  {'BUY':DirData,'SELL':DirData} pooled over all stocks -> the
                 prior. per_sym_data {sym:{'BUY':DirData,'SELL':DirData}}.
    Each stock's fit is shrunk toward the global prior, then gated per stock by
    its own walk-forward vs the current (default) config. Returns
    {sym: {'BUY':DirResult,'SELL':DirResult}}."""
    # 1) global prior coefficients per direction
    prior = {}
    for d in ("BUY", "SELL"):
        gd = global_data.get(d)
        wins = int(gd.y.sum()) if gd and gd.n else 0
        if gd and gd.n >= min_samples and wins >= min_per_class \
                and (gd.n - wins) >= min_per_class:
            _, beta = _fit_logistic(gd.X, gd.y, l2=l2)
            prior[d] = beta
        else:
            prior[d] = np.zeros(len(FAMILIES))

    # 2) per-stock fits shrunk toward the prior
    out: dict = {}
    for sym, dirs in per_sym_data.items():
        out[sym] = {}
        for d in ("BUY", "SELL"):
            dd = dirs[d]
            defw = defaults.get(d, {})
            res = DirResult(direction=d, trained=False, n=dd.n,
                            win_rate=round(dd.win_rate, 4),
                            availability=dd.availability,
                            prev_weights={k: defw.get(k, 1.0) for k in FAMILIES})
            wins = int(dd.y.sum())
            losses = dd.n - wins
            if dd.n < min_samples or wins < min_per_class or losses < min_per_class:
                res.reason = (f"insufficient per-stock data "
                              f"(n={dd.n}, {wins}W/{losses}L)")
                out[sym][d] = res
                continue
            intercept, beta = _fit_logistic(dd.X, dd.y, l2=l2, prior=prior[d])
            p = _sigmoid(intercept + dd.X @ beta)
            res.intercept = round(float(intercept), 4)
            res.coefs = {FAMILIES[j]: round(float(beta[j]), 4)
                         for j in range(len(FAMILIES))}
            res.metrics = _metrics(dd.y, p)
            res.weights = _coefs_to_weights(beta, dd.availability, defw, floor)
            res.trained = True
            from .validate import walk_forward
            res.oos = walk_forward(dd, defw, l2=l2)
            res.beats_baseline = bool(res.oos.get("beats_baseline"))
            res.reason = ("ok — beats default OOS" if res.ship
                          else "fit ok but does NOT beat default OOS")
            out[sym][d] = res
    return out


def current_default_weights() -> dict:
    """Read the family defaults straight off the built agent tree, per
    direction: {'BUY': {family: w}, 'SELL': {family: w}}. Used as the fallback
    and the 'before' column in reports."""
    try:
        from quant.registry import build_root
        root = build_root()
        buy, sell = {}, {}
        for child in root.children:
            if child.key in FAMILIES:
                buy[child.key] = float(child.default_weight_buy)
                sell[child.key] = float(child.default_weight_sell)
        return {"BUY": buy, "SELL": sell}
    except Exception:
        return {"BUY": {k: 1.0 for k in FAMILIES},
                "SELL": {k: 1.0 for k in FAMILIES}}
