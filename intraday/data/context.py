"""IntradayContext — the interface the scanner/agents call in ALL three modes.

The signatures here are the single-code-path contract (see plan Q5). LiveContext
is backed by the Gateway (chain/spot/greeks/positioning now; the rolling bar
buffer fed by the WS tick-aggregator lands with the session loop in Phase 2).
BacktestContext replays the historical dataset with BS-synthesised legs (Phase 4).
"""
from __future__ import annotations

import datetime as dt

from ..options import Chain, build_chain, greeks_for, ist_now
from ..options.models import IST, MARKET_CLOSE, MARKET_OPEN


class IntradayContext:
    """Abstract point-in-time data source. Same method surface everywhere."""

    # --- clock / universe ---
    def now(self) -> dt.datetime: raise NotImplementedError
    def symbols(self) -> list[str]: raise NotImplementedError

    def session_bounds(self, day: dt.date | None = None) -> tuple[dt.datetime, dt.datetime]:
        day = day or self.now().date()
        return (dt.datetime.combine(day, MARKET_OPEN, IST),
                dt.datetime.combine(day, MARKET_CLOSE, IST))

    def is_square_off(self, now: dt.datetime | None = None) -> bool:
        raise NotImplementedError

    # --- underlying bars (Phase 2 fills the live rolling buffer) ---
    def bars(self, symbol: str, tf: str = "5m"): raise NotImplementedError
    def slice(self, symbol: str, as_of: dt.datetime, tf: str = "5m"): raise NotImplementedError
    def spot(self, symbol: str) -> float: raise NotImplementedError
    def vwap(self, symbol: str) -> float: raise NotImplementedError

    # --- option chain / greeks ---
    def chain(self, symbol: str, expiry_index: int = 0) -> Chain: raise NotImplementedError
    def leg_greeks(self, chain: Chain, strike: float, right: str) -> dict:
        q = chain.get(strike, str(right).upper().startswith("C"))
        return greeks_for(q, chain) if q else {}
    def positioning(self, symbol: str) -> dict: raise NotImplementedError


class LiveContext(IntradayContext):
    """Backed by the broker Gateway. Options + session reads are implemented;
    the underlying bar buffer (bars/slice/vwap) is wired to the WS tick
    aggregator in Phase 2 — until then those raise a clear NotImplementedError."""

    def __init__(self, client, cfg: dict):
        self.client = client
        self.cfg = cfg
        self._last_chain: dict[str, Chain] = {}
        from .bars import BarBuilder
        self.bar_builder = BarBuilder(cfg.get("bar_timeframe", "5m"))
        self._vix: float | None = None
        self._prev_day: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}      # symbol -> "EXCH|TOKEN" (underlying)

    # clock / universe
    def now(self) -> dt.datetime:
        return ist_now()

    def symbols(self) -> list[str]:
        return list(self.cfg.get("universe", ["NIFTY", "BANKNIFTY"]))

    def is_square_off(self, now: dt.datetime | None = None) -> bool:
        now = now or self.now()
        hhmm = self.cfg.get("square_off_time", "15:15")
        try:
            h, m = (int(x) for x in str(hhmm).split(":"))
        except ValueError:
            h, m = 15, 15
        return now.timetz() >= dt.time(h, m, tzinfo=IST)

    # options
    def chain(self, symbol: str, expiry_index: int = 0) -> Chain:
        expiry = ""
        if expiry_index > 0:
            payload0 = self.client.option_chain(
                symbol, self.cfg.get("underlying_exchange", "NSE"), count=0)
            expiries = payload0.get("expiries", [])
            if expiry_index < len(expiries):
                expiry = expiries[expiry_index]
        payload = self.client.option_chain(
            symbol, self.cfg.get("underlying_exchange", "NSE"),
            expiry=expiry, count=int(self.cfg.get("atm_window_strikes", 10)))
        ch = build_chain(payload, symbol)
        self._last_chain[symbol] = ch
        return ch

    def positioning(self, symbol: str) -> dict:
        sweep = self.client.oi_sweep(
            [symbol], strikes=int(self.cfg.get("atm_window_strikes", 10)))
        rows = sweep if isinstance(sweep, list) else \
            sweep.get("results", sweep.get("data", []))
        for r in (rows or []):
            if isinstance(r, dict) and r.get("symbol", symbol) == symbol:
                return r
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def vix(self) -> float | None:
        return self._vix

    def set_vix(self, v: float | None) -> None:
        self._vix = v

    def prev_day(self, symbol: str) -> dict:
        return self._prev_day.get(symbol, {})

    def set_prev_day(self, symbol: str, levels: dict) -> None:
        self._prev_day[symbol] = levels

    # bars — fed by the WS tick aggregator (GatewayWS.on_tick -> on_tick here)
    def on_tick(self, symbol_token: str, msg: dict) -> None:
        """Route a touchline WS frame into the bar builder, keyed by the
        underlying symbol we mapped this token to (set via register_token)."""
        sym = self._sym_for_token(symbol_token)
        if sym is None:
            return
        self.bar_builder.on_tick(sym, msg.get("lp"), msg.get("v"),
                                 float(msg.get("ft") or 0) or self.now().timestamp())

    def register_token(self, symbol: str, exch_token: str) -> None:
        self._tokens[exch_token] = symbol

    def _sym_for_token(self, exch_token: str) -> str | None:
        return self._tokens.get(exch_token)

    def seed_bars(self, symbol: str, candles: list) -> None:
        self.bar_builder.seed(symbol, candles)

    def bars(self, symbol: str, tf: str = "5m"):
        return self.bar_builder.bars(symbol)

    def spot(self, symbol: str) -> float:   # override: prefer the live tick
        s = self.bar_builder.spot(symbol)
        if s:
            return s
        ch = self._last_chain.get(symbol)
        return ch.spot if ch else 0.0

    def vwap(self, symbol: str) -> float:
        b = self.bar_builder.bars(symbol)
        if b is not None and "vwap" in b and len(b):
            return float(b["vwap"].iloc[-1])
        return 0.0


class BacktestContext(IntradayContext):
    """Replays the historical dataset (~/Downloads/Options_data): underlying
    5-min candles are real; option legs are BS-synthesised from that day's
    atm_iv/skew (the dataset is EOD-only on options). Built in Phase 4."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "BacktestContext lands in Phase 4 (backtest runner). The interface "
            "it implements is defined here so the scanner/agents code is written "
            "against it now and runs unchanged in backtest.")
