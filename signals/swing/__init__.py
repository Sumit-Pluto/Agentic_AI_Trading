"""signals/swing/ — Rbknox (Rob Booker Knoxville Divergence) + Order-Block swing.

The three engines below are vendored **byte-identical** from the SM Agent project
(``…/Trading_Project/Agents/SM Agent/intraday/app/services``) — pure numpy/pandas
that take an OHLCV DataFrame, so they are fed Shoonya candles (no yfinance):

  * ``knoxville``          — Rob Booker "Knoxville Divergence" reversal (`compute_knoxville`)
  * ``market_structure``   — ATR-ZigZag major swings (HH/HL/LH/LL)
  * ``ob_reversal``        — break-of-structure + Order-Block zone + retrace tap (`detect`)

``engine.swing_signal`` adds the fusion the source never had: a Knoxville reversal
firing AT the OB retrace tap is the swing trigger (decision #2). See
docs/SWING_MCX_OI_MANUAL_PLAN.md §4.
"""
from __future__ import annotations

from signals.swing.engine import SwingCandidate, swing_signal

__all__ = ["SwingCandidate", "swing_signal"]
