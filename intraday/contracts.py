"""INTRADAY — frozen inter-module contracts (ported from swing_hyena, adapted).

Every module implements against THESE types. Single code path rule:
backtest == paper == live. The ONLY things that change are the Broker
implementation and the IntradayContext data source; scanner/agents/risk/rules/
exits run byte-for-byte the same in all three modes.

Adaptations vs swing:
  • timestamps are intraday (dt.datetime), not just dates
  • Signal carries the *instrument* (option leg) to trade, not just the underlying
  • Position carries option fields (strike/right/expiry/lot/entry_prem/entry_spot)
  • agent families are R/S/F/V/M/C (Regime/Setup/Flow-OI/Volatility/Momentum/Catalyst)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Intraday agent families (see intraday.agents). Regime (R) runs once/tick on
# the index; the rest run per candidate. Catalyst (C) spans both.
FAMILIES = ("R", "S", "F", "V", "M", "C")
# The families the brain weights per-candidate (Regime is a gate/scalar, not a score).
STOCK_FAMILIES = ("S", "F", "V", "M", "C")


# ------------------------------------------------------------------ signals
@dataclass
class Signal:
    ts: dt.datetime                   # intraday decision time (IST)
    symbol: str                       # underlying, e.g. "NIFTY"
    direction: str                    # "BUY" | "SELL" (of the underlying view)
    score_buy: float
    score_sell: float
    family_scores: dict[str, float]   # {"S":.., "F":.., "V":.., "M":.., "C":..}
    agent_rows: list[dict]            # per-agent {agent,family,score_buy,score_sell,na,veto,shadow,detail}
    vetoes: list[str]
    n_scored: int
    regime: dict                      # {"on":bool, "scalar":float, "detail":str, "vetoes":[..]}
    brain_version: str = "equal-v0"
    # option instrument chosen to express the view (filled by the strategy layer)
    instrument: dict | None = None    # {token, tsym, exch, strike, right, expiry, lot_size, entry_prem}

    @property
    def composite(self) -> float:
        return self.score_buy if self.direction == "BUY" else self.score_sell

    @property
    def date(self) -> dt.date:
        return self.ts.date()


# ------------------------------------------------------------------ orders
@dataclass
class OrderIntent:
    symbol: str                       # tradable instrument tradingsymbol (the option leg)
    side: str                         # "BUY" | "SELL"
    qty: int                          # in SHARES (lots * lot_size)
    order_type: str = "MARKETABLE_LIMIT"
    limit_px: float | None = None
    reason: str = ""                  # "entry:signal" | "exit:I1 stop" | "exit:I0 squareoff" ...
    signal_id: int | None = None
    signal_ts: float = 0.0            # perf-clock stamp when the signal fired (latency gate, §7)
    # option routing context (needed to place on the Gateway / net positions)
    exch: str = "NFO"
    token: str = ""
    underlying: str = ""              # e.g. "NIFTY" — for per-symbol dedup/exposure rules
    strike: float = 0.0
    right: str = ""                   # "CE" | "PE"
    expiry: dt.date | None = None
    lot_size: int = 0
    strategy: str = "default"         # which entry template produced this (for §8 attribution + reporting)


@dataclass
class Fill:
    order_id: int
    symbol: str
    side: str
    qty: int
    px: float
    ts: float
    date: dt.date


class Broker:
    """intraday.brokers implement this. PaperBroker fills at a marketable price
    +/- slippage; GatewayBroker routes to the broker Gateway (Shoonya)."""
    name = "abstract"

    def place(self, intent: "OrderIntent") -> dict: ...      # -> {broker_order_id, status, fill_px?}
    def cancel(self, broker_order_id: str) -> bool: ...
    def positions(self) -> list[dict]: ...


# ------------------------------------------------------------------ positions
@dataclass
class Position:
    symbol: str                       # option tradingsymbol
    qty: int                          # SHARES (signed by side handled via `side`)
    side: str                         # "BUY" (long premium) | "SELL" (short premium)
    entry_px: float                   # option premium per share at entry
    entry_ts: dt.datetime
    stop: float                       # UNDERLYING stop level (primary intraday stop)
    risk_per_share: float             # premium at risk per share
    # option identity
    strike: float = 0.0
    right: str = ""                   # "CE" | "PE"
    expiry: dt.date | None = None
    lot_size: int = 0
    exch: str = "NFO"
    token: str = ""
    underlying: str = ""              # e.g. "NIFTY"
    entry_spot: float = 0.0           # underlying at entry (for R-multiple + BS reprice)
    delta_at_entry: float = 0.0
    # intraday exit-machine state
    age_bars: int = 0                 # bars held (not sessions)
    max_prem: float = 0.0             # highest favourable premium seen (trailing)
    max_fav_spot: float = 0.0         # highest favourable underlying move seen
    breakeven_done: bool = False      # stop moved to breakeven after +1R (I3)
    stall_checked: bool = False       # I5 one-shot theta-stall guard
    partial_done: bool = False        # T1 partial booked (I3)
    strategy: str = "default"
    position_id: int | None = None
    exit_reason: str | None = None
    exit_px: float | None = None
    exit_ts: dt.datetime | None = None
    log: list = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "BUY"


# ------------------------------------------------------------------ brain
@dataclass
class Brain:
    """A trained decision layer. v0 = equal weights (DeMiguel baseline).
    family_w: weights over the per-candidate families (S,F,V,M,C) summing to 1.
    agent_w:  optional per-agent weight overrides (post-pruning).
    symbol_residual: {symbol: {"delta": float, "n": int}} — lambda(n)-shrunk tilt,
        applied as score += lambda(n)*delta, lambda = n/(n+200)."""
    version: str
    family_w: dict[str, float]
    agent_w: dict[str, float] = field(default_factory=dict)
    symbol_residual: dict[str, dict] = field(default_factory=dict)
    trained_at: str = ""
    train_window: str = ""
    metrics: dict = field(default_factory=dict)

    @staticmethod
    def equal(version: str = "equal-v0") -> "Brain":
        fams = STOCK_FAMILIES
        return Brain(version=version, family_w={f: 1 / len(fams) for f in fams})

    def lam(self, symbol: str) -> float:
        n = self.symbol_residual.get(symbol, {}).get("n", 0)
        return n / (n + 200.0)
