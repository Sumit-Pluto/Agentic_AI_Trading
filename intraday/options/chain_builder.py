"""Build a greeks-complete Chain from a Gateway /api/option-chain payload.

Ported from OptionSmith (chain/loaders.py). The mapping is where a field-name
mistake would silently mis-price a whole chain, so it is split out and testable
against a captured payload with no broker session. Carry is calibrated from the
chain's own put-call parity BEFORE any IV is inverted — inverting against the
wrong forward manufactures a phantom call-over-put skew (see calibrate_carry).
"""
from __future__ import annotations

import math

from .mathx import (bs_delta, bs_gamma, bs_theta, bs_vega, implied_vol)
from .models import Chain, OptionQuote, display_qty_to_shares, ist_now

import datetime as dt

# carry band (annualised) — a rate outside this is a stale/crossed quote, not carry
_CARRY_MIN, _CARRY_MAX = -0.35, 0.60
_CARRY_MIN_ESTIMATES = 3
_BASIS_MAX = 0.05
_ANNUALISE_MIN_DTE = 7
_DUP_TICK = 0.05


def _num(v, default: float = 0.0) -> float:
    """Tolerant finite float for Gateway fields (proxies hand back strings /
    NaN / '1,234.5'). Anything non-finite becomes default (0 = 'not quoted')."""
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else default
    if v is None:
        return default
    try:
        f = float(str(v).replace(",", "").strip() or default)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _inum(v, default: int = 0) -> int:
    return int(_num(v, float(default)))


def _payload_rows(d: dict) -> list[dict]:
    rows = d.get("chain")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def from_gateway_payload(d: dict, symbol: str | None = None) -> Chain:
    """Build a Chain from a Gateway `/api/option-chain` response.

    Legs the Gateway could not quote (`quoted` false, no price, no strike) are
    dropped rather than admitted at zero — a zero-priced leg reads as free
    optionality. Shoonya sends no IV; it is inverted on load (fill_missing_ivs).
    """
    spot = _num(d.get("spot"))
    expiry_iso = d.get("expiry_iso") or ""
    if not expiry_iso:
        raise ValueError(f"gateway returned no usable expiry for "
                         f"{d.get('symbol', symbol)} (expiry={d.get('expiry')!r})")
    try:
        expiry = dt.date.fromisoformat(str(expiry_iso))
    except (TypeError, ValueError):
        raise ValueError(f"gateway returned an unreadable expiry {expiry_iso!r} "
                         f"for {d.get('symbol', symbol)}") from None
    if spot <= 0:
        raise ValueError(
            f"gateway could not resolve the underlying price for "
            f"{d.get('symbol', symbol)} — every strike would be judged against "
            f"spot 0, so no read of this chain would mean anything")

    exchange = (d.get("exchange") or "NFO").strip().upper()
    lot_hint = _inum(d.get("lot_size"))

    quotes: list[OptionQuote] = []
    for row in _payload_rows(d):
        for key, is_call in (("CE", True), ("PE", False)):
            leg = row.get(key)
            if not isinstance(leg, dict) or not leg.get("quoted"):
                continue
            ltp, bid, ask = _num(leg.get("ltp")), _num(leg.get("bid")), _num(leg.get("ask"))
            if ltp <= 0 and bid <= 0 and ask <= 0:
                continue
            strike = _num(leg.get("strike") or row.get("strike"))
            if strike <= 0:
                continue
            leg_lot = _inum(leg.get("lot_size")) or lot_hint
            quotes.append(OptionQuote(
                strike=strike, is_call=is_call, ltp=ltp, bid=bid, ask=ask,
                oi=_inum(leg.get("oi_num")), prev_oi=_inum(leg.get("prev_oi")),
                volume=_inum(leg.get("volume")),
                prev_close=_num(leg.get("prev_close")),
                bid_qty=display_qty_to_shares(leg.get("bid_qty"), exchange, leg_lot),
                ask_qty=display_qty_to_shares(leg.get("ask_qty"), exchange, leg_lot),
                fetched_at=_num(leg.get("fetched_at")) or None,
                token=str(leg.get("token") or ""),
                tsym=str(leg.get("tsym") or ""),
                lot_size=leg_lot,
                iv=None))

    lot = _inum(d.get("lot_size"))
    q = d.get("quality") if isinstance(d.get("quality"), dict) else {}
    return Chain(symbol=d.get("symbol") or symbol or "", spot=spot,
                 expiry=expiry, lot_size=lot, quotes=_dedup(quotes),
                 asof=ist_now(), exchange=exchange,
                 quote_span_s=_num(q.get("quote_span_s")),
                 built_at=_num(q.get("built_at")) or None,
                 source=f"gateway:{d.get('exchange', '')}")


def _dedup(quotes: list[OptionQuote]) -> list[OptionQuote]:
    """One quote per (strike, right). Agreeing duplicates (books within a tick)
    keep the first; disagreeing ones keep the freshest stamp, else drop the pair
    (a leg whose price is two numbers at once was not quoted coherently)."""
    by_key: dict[tuple[float, bool], list[OptionQuote]] = {}
    for q in quotes:
        by_key.setdefault((q.strike, q.is_call), []).append(q)
    if all(len(v) == 1 for v in by_key.values()):
        return quotes
    out: list[OptionQuote] = []
    for dups in by_key.values():
        if len(dups) == 1:
            out.append(dups[0]); continue
        first = dups[0]
        if all(abs(q.bid - first.bid) <= _DUP_TICK
               and abs(q.ask - first.ask) <= _DUP_TICK for q in dups[1:]):
            out.append(first); continue
        stamps = [q.fetched_at or 0.0 for q in dups]
        newest = max(stamps)
        if newest > 0 and stamps.count(newest) == 1:
            out.append(dups[stamps.index(newest)])
    return out


def calibrate_carry(chain: Chain) -> Chain:
    """Set chain.carry_rate from the chain's own put-call parity (median of
    near-the-money C-P+K forwards). MUST run before any IV inversion."""
    if chain.carry_rate is not None or not (chain.spot > 0) \
            or not math.isfinite(chain.spot):
        return chain
    t = chain.t_years
    if t <= 0:
        return chain
    fwd, _n = chain.implied_forward(min_estimates=_CARRY_MIN_ESTIMATES)
    if not fwd or fwd <= 0:
        return chain
    basis = math.log(fwd / chain.spot)
    if abs(basis) > _BASIS_MAX:
        return chain
    r = basis / t
    if (chain.days_to_expiry >= _ANNUALISE_MIN_DTE
            and not (_CARRY_MIN <= r <= _CARRY_MAX)):
        return chain
    chain.carry_rate = r
    return chain


def fill_missing_ivs(chain: Chain) -> Chain:
    """Calibrate carry, then invert BS for any quote without a (finite, >0) IV."""
    calibrate_carry(chain)
    t, r = chain.t_years, chain.r
    for q in chain.quotes:
        if q.iv is None or not math.isfinite(q.iv) or not (q.iv > 0):
            q.iv = implied_vol(q.is_call, q.mid, chain.spot, q.strike, t, r)
    return chain


def greeks_for(quote: OptionQuote, chain: Chain) -> dict:
    """Per-share greeks for one leg at the chain's spot/carry/time. Theta is
    returned per-day (per-year / 365) — the useful intraday figure."""
    iv = quote.iv
    if iv is None or not (iv > 0):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "iv": None}
    s, k, t, r = chain.spot, quote.strike, chain.t_years, chain.r
    return {
        "delta": bs_delta(quote.is_call, s, k, t, iv, r),
        "gamma": bs_gamma(s, k, t, iv, r),
        "theta": bs_theta(quote.is_call, s, k, t, iv, r) / 365.0,
        "vega": bs_vega(s, k, t, iv, r) / 100.0,   # per vol-point
        "iv": iv,
    }


def build_chain(payload: dict, symbol: str | None = None) -> Chain:
    """Gateway payload -> Chain with carry calibrated and greeks/IV filled."""
    return fill_missing_ivs(from_gateway_payload(payload, symbol))
