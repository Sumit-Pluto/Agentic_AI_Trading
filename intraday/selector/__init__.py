"""Futures-vs-options instrument selector (pure functions, no I/O).

Sits between the scanner's directional signal and the Governor/risk sizing:
for each firing signal it decides whether the view is better expressed as a
long option leg (OPT) or a futures position (FUT), so the engine keeps an edge
in both trending/high-IV/illiquid/late-session regimes (futures) and
defined-risk/cheap-IV/range regimes (options).

Single-code-path rule: identical logic in backtest/paper/live — only the data
source (ctx) and broker change around it.
"""
from .features import SelectorFeatures, build_features
from .router import Decision, choose, select

__all__ = ["SelectorFeatures", "Decision", "build_features", "choose", "select"]
