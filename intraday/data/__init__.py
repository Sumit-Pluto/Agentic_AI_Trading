"""Market-data context — the single-code-path seam.

The scanner and every agent call ONLY the IntradayContext interface, so the same
code runs in backtest, paper and live: only the concrete context (and the clock)
changes. LiveContext is fed by the Gateway; BacktestContext (Phase 4) replays the
historical dataset with BS-synthesised option legs.
"""
from .context import BacktestContext, IntradayContext, LiveContext

__all__ = ["IntradayContext", "LiveContext", "BacktestContext"]
