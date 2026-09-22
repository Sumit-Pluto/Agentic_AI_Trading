"""quant/pipeline/contracts.py — the data contracts every layer shares.

A strategy plug emits a TriggerEvent; enrichment turns it into a Candidate (event +
feature vector); the labeler attaches an outcome (train time); the model attaches
P(win); filters attach pass/factor; the selector ranks. One shape end-to-end so the
pipeline is strategy/filter/feature agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TriggerEvent:
    """What a strategy plug proposes. Carries its OWN barrier geometry."""
    strategy_id: str
    setup_id: str
    symbol: str
    direction: str            # "BUY" / "SELL"
    bar_time: str             # ISO timestamp of the signal bar
    entry_price: float
    stop_atr: float = 1.0     # stop = entry -/+ stop_atr * ATR   (lower barrier)
    target_atr: float = 1.5   # target = entry +/- target_atr * ATR (upper barrier)
    horizon_bars: int = 12    # vertical barrier (bars) / EOD, whichever first
    extras: dict = field(default_factory=dict)


@dataclass
class Candidate:
    """A trigger enriched with features (+ label at train time, +P(win) at infer)."""
    event: TriggerEvent
    features: dict = field(default_factory=dict)
    # train-time:
    label: int | None = None            # 1 win / 0 loss
    outcome_pnl_pct: float | None = None
    exit_reason: str | None = None
    # infer-time:
    p_win: float | None = None          # calibrated
    filter_factor: float = 1.0          # product of multiplier filters
    vetoed_by: str | None = None        # first veto filter id, or None
    size: float = 0.0

    @property
    def alive(self) -> bool:
        return self.vetoed_by is None


@dataclass
class FilterResult:
    passed: bool                 # False = veto
    factor: float = 1.0          # multiplier filters scale P(win)/size
    reason: str = ""
