"""quant/pipeline/selector.py — LAYER F portfolio selector & sizing.

Ranks the surviving candidates across ALL strategy plugs by calibrated P(win) x
filter multiplier, keeps the top-K per budget, and sizes ∝ (score − p*) (vol-target /
Kelly-cap in production). This is where "trade less, trade the best" happens.
"""

from __future__ import annotations


def select(candidates, p_star=0.55, top_k=5, max_size=1.0):
    alive = [c for c in candidates if c.alive and c.p_win is not None]
    for c in alive:
        c.score = c.p_win * c.filter_factor
    ranked = sorted(alive, key=lambda c: -c.score)
    chosen = [c for c in ranked if c.score >= p_star][:top_k]
    for c in chosen:
        c.size = round(min(max_size, max(0.0, (c.score - p_star) * 4.0)), 3)
    return chosen
