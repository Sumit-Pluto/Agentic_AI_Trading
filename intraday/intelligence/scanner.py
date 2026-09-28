"""Per-bar scanner (PDF §2.4). Ported cadence from swing_hyena's Scanner:

  1. build the regime AgentInput from the INDEX bars/chain; run family R (+R6);
     fold to a regime dict via combiner.regime_gate
  2. if regime is off (or vetoed), emit no signals
  3. per candidate: build its AgentInput, run S/F/V/M/C; combine_stock across the
     candidates (cross-sectional), resolve_direction at the (regime-aware) threshold
  4. for each firing candidate, pick the option leg to express the view

Data comes through an IntradayContext-shaped `ctx`; the scanner computes the
session context (opening range, first hour, breadth) itself from the bars.
"""
from __future__ import annotations

import datetime as dt

from ..agents.base import AgentInput, run_family
from ..contracts import Brain, Signal
from .combiner import combine_stock, regime_gate, resolve_direction

_TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}


def _tf_minutes(cfg: dict) -> int:
    return _TF_MIN.get(str(cfg.get("bar_timeframe", "5m")), 5)


def _session_ctx(bars, cfg: dict, extra: dict | None = None) -> dict:
    """Opening-range / first-hour / elapsed-minutes context from the session's
    bars (which start at the open, so bar count * tf ≈ minutes elapsed)."""
    s = dict(extra or {})
    tf = _tf_minutes(cfg)
    if bars is None or len(bars) == 0:
        return s
    n = len(bars)
    s.setdefault("minutes_since_open", n * tf)
    open_hhmm = str(cfg.get("session_open", "09:15")); close_hhmm = str(cfg.get("session_close", "15:30"))
    try:
        oh, om = (int(x) for x in open_hhmm.split(":")); ch, cm = (int(x) for x in close_hhmm.split(":"))
        total = (ch * 60 + cm) - (oh * 60 + om)
        s.setdefault("minutes_to_close", max(total - n * tf, 0))
    except ValueError:
        pass
    s.setdefault("day_open", float(bars["open"].iloc[0]))
    or_bars = max(1, cfg.get("or_minutes", 15) // tf)
    s.setdefault("or_minutes", cfg.get("or_minutes", 15))
    s.setdefault("or_hi", float(bars["high"].iloc[:or_bars].max()))
    s.setdefault("or_lo", float(bars["low"].iloc[:or_bars].min()))
    fh_bars = max(1, 60 // tf)
    if n >= fh_bars:
        s.setdefault("first_hour_hi", float(bars["high"].iloc[:fh_bars].max()))
        s.setdefault("first_hour_lo", float(bars["low"].iloc[:fh_bars].min()))
    return s


def select_instrument(chain, direction: str, cfg: dict) -> dict | None:
    """Pick the option leg to express a directional view: a BUY view buys a call,
    a SELL view buys a put (long premium — the Phase-1 structure). Moneyness from
    cfg['entry_moneyness'] ('ATM' default; +N/-N steps toward OTM/ITM). Requires a
    two-sided book (executable ask)."""
    if chain is None or not chain.strikes:
        return None
    is_call = direction == "BUY"
    step = chain.strike_step
    offset = int(cfg.get("entry_moneyness_steps", 0))
    target = chain.atm + offset * step * (1 if is_call else -1)
    # nearest listed strike to target with an executable ask
    cands = sorted(chain.strikes, key=lambda k: abs(k - target))
    for k in cands:
        q = chain.get(k, is_call)
        if q and q.executable(1) and q.ask > 0:
            from ..options import greeks_for
            g = greeks_for(q, chain)
            return {"token": q.token, "tsym": q.tsym, "exch": chain.exchange,
                    "strike": q.strike, "right": q.right,
                    "expiry": chain.expiry.isoformat(), "lot_size": chain.lot_size or q.lot_size,
                    "entry_prem": q.ask, "bid": q.bid, "ask": q.ask,
                    "delta": g.get("delta"), "iv": q.iv}
    return None


class Scanner:
    def __init__(self, ctx, brain: Brain | None = None, cfg: dict | None = None):
        self.ctx = ctx
        self.brain = brain or Brain.equal()
        self.cfg = cfg or {}

    def _index_symbol(self) -> str:
        return getattr(self.ctx, "index_symbol", None) or self.cfg.get("index_symbol", "NIFTY")

    def _build_input(self, symbol: str, index_bars, session_extra=None) -> AgentInput:
        ctx = self.ctx
        bars = ctx.bars(symbol)
        chain = None
        try:
            chain = ctx.chain(symbol)
        except Exception:
            chain = None
        spot = 0.0
        try:
            spot = ctx.spot(symbol) or (chain.spot if chain else 0.0)
        except Exception:
            spot = chain.spot if chain else 0.0
        positioning = {}
        try:
            positioning = ctx.positioning(symbol) or {}
        except Exception:
            positioning = {}
        vix = None
        try:
            vix = ctx.vix()
        except Exception:
            vix = None
        prev_day = {}
        try:
            prev_day = ctx.prev_day(symbol) or {}
        except Exception:
            prev_day = {}
        session = _session_ctx(bars, self.cfg, session_extra)
        return AgentInput(symbol=symbol, bars=bars, index_bars=index_bars, chain=chain,
                          spot=spot, now=ctx.now(), cfg=self.cfg, vix=vix,
                          prev_day=prev_day, session=session, positioning=positioning)

    def _breadth(self, symbols: list[str]) -> float | None:
        """% of candidates trading above their session VWAP — market breadth."""
        above = tot = 0
        for s in symbols:
            try:
                b = self.ctx.bars(s)
                if b is None or len(b) == 0 or "vwap" not in b:
                    continue
                tot += 1
                if float(b["close"].iloc[-1]) > float(b["vwap"].iloc[-1]):
                    above += 1
            except Exception:
                continue
        return (above / tot * 100.0) if tot else None

    def scan(self) -> tuple[list[Signal], dict, list[dict]]:
        cfg = self.cfg
        idx = self._index_symbol()
        index_bars = self.ctx.bars(idx)
        candidates = [s for s in self.ctx.symbols() if s != idx] or [idx]

        # 1. regime pass on the index
        breadth = self._breadth([idx] + candidates)
        reg_in = self._build_input(idx, index_bars, {"breadth_pct": breadth})
        r_results = run_family(reg_in, "R")
        regime = regime_gate(r_results)

        all_rows: list[dict] = [self._row(idx, r) for r in r_results]
        if not regime["on"]:
            return [], regime, all_rows

        # 2. per-candidate scan
        results_by_symbol: dict[str, list] = {}
        for sym in candidates:
            inp = self._build_input(sym, index_bars)
            rs = (run_family(inp, "S") + run_family(inp, "F")
                  + run_family(inp, "V") + run_family(inp, "M") + run_family(inp, "C"))
            results_by_symbol[sym] = rs
            all_rows.extend(self._row(sym, r) for r in rs)

        combined = combine_stock(results_by_symbol, self.brain)

        # 3. resolve + pick instrument
        threshold = float(cfg.get("score_threshold", 60.0))
        margin = float(cfg.get("score_margin", 10.0))
        # regime tightens the bar when conviction is low (scalar<1)
        eff_threshold = threshold + (1.0 - regime.get("scalar", 1.0)) * 10.0
        now = self.ctx.now()
        signals: list[Signal] = []
        for sym, sc in combined.items():
            direction = resolve_direction(sc, eff_threshold, margin)
            if not direction:
                continue
            chain = None
            try:
                chain = self.ctx.chain(sym)
            except Exception:
                chain = None
            instrument = select_instrument(chain, direction, cfg)
            if instrument is None:
                continue                        # no tradable leg — skip
            signals.append(Signal(
                ts=now, symbol=sym, direction=direction,
                score_buy=sc.get("score_buy") or 0.0, score_sell=sc.get("score_sell") or 0.0,
                family_scores=(sc.get("family_buy") if direction == "BUY" else sc.get("family_sell")) or {},
                agent_rows=[self._row(sym, r) for r in results_by_symbol[sym]],
                vetoes=sc.get("vetoes", []), n_scored=sc.get("n_scored", 0),
                regime=regime, brain_version=self.brain.version, instrument=instrument))
        return signals, regime, all_rows

    @staticmethod
    def _row(symbol: str, r) -> dict:
        return {"symbol": symbol, "agent": r.agent, "family": r.family,
                "score_buy": r.score_buy, "score_sell": r.score_sell,
                "na": r.na, "veto": r.veto, "veto_long": r.veto_long,
                "veto_short": r.veto_short, "shadow": r.shadow, "detail": r.detail}
