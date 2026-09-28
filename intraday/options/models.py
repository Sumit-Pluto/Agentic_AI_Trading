"""Option-chain data types — OptionQuote + Chain — and NSE session/date helpers.

Ported from OptionSmith (core/models.py), trimmed to what the intraday engine
needs: the strategist-only Leg/StrategyResult structure types are omitted. All
prices are per SHARE in rupees; multiply by lot_size for per-lot rupees.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .mathx import RISK_FREE

# Exchanges that report displayed depth size in LOTS rather than SHARES (MCX);
# NSE/NFO report shares. Kept so the engine has one units convention internally.
DISPLAY_QTY_IN_LOTS = frozenset({"MCX"})
# Options written on FUTURES need Black-76, not spot Black-Scholes. The intraday
# engine targets NSE index/stock options (spot BS); MCX is flagged, not priced.
_OPTIONS_ON_FUTURES = frozenset({"MCX", "NCDEX"})


def display_qty_to_shares(qty, exchange: str, lot_size: int) -> int:
    """Normalise a feed's displayed size to SHARES, whatever it reported in."""
    try:
        q = int(float(qty or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    if q <= 0 or q > 0x7FFFFFFF:
        return 0  # corrupt / unknown depth is 0, never "infinite"
    if (exchange or "").strip().upper() in DISPLAY_QTY_IN_LOTS:
        lot = int(lot_size or 0)
        return q * lot if lot > 0 else 0
    return q


@dataclass
class OptionQuote:
    """One tradable option contract from the chain (per-share prices)."""
    strike: float
    is_call: bool
    ltp: float                      # last / mid reference price per share
    bid: float = 0.0
    ask: float = 0.0
    oi: int = 0                     # open interest (contracts)
    prev_oi: int = 0                # 0 = UNKNOWN, never "unchanged"
    volume: int = 0
    iv: float | None = None         # decimal (0.32 = 32%), inverted if absent
    prev_close: float = 0.0         # yesterday's close for THIS contract
    bid_qty: int = 0                # shares resting AT the touch, not lots
    ask_qty: int = 0
    fetched_at: float | None = None # epoch secs this leg was received (legs not contemporaneous)
    token: str = ""                 # broker instrument token (EXCH|TOKEN resolvable)
    tsym: str = ""                  # broker tradingsymbol
    lot_size: int = 0

    @property
    def right(self) -> str:
        return "CE" if self.is_call else "PE"

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return 0.5 * (self.bid + self.ask)
        return self.ltp

    @property
    def spread_pct(self) -> float:
        """Round-trip spread as % of mid — the liquidity gate."""
        if self.bid > 0 and self.ask > self.bid and self.mid > 0:
            return (self.ask - self.bid) / self.mid * 100.0
        return 0.0

    def exec_price(self, side: int) -> float:
        """EXECUTABLE price: buy at ask, sell at bid. 0.0 when that side is empty."""
        if side > 0:
            return self.ask if self.ask > 0 else 0.0
        return self.bid if self.bid > 0 else 0.0

    def executable(self, side: int) -> bool:
        return self.exec_price(side) > 0.0

    @property
    def d_oi(self) -> int:
        return int(self.oi - self.prev_oi)

    @property
    def d_oi_pct(self) -> float:
        return (self.d_oi / self.prev_oi * 100.0) if self.prev_oi > 0 else 0.0


@dataclass
class Chain:
    """A single-expiry option chain plus its underlying context."""
    symbol: str
    spot: float
    expiry: dt.date
    lot_size: int
    quotes: list[OptionQuote] = field(default_factory=list)
    asof: dt.datetime | None = None   # intraday: full timestamp, not just date
    exchange: str = "NFO"
    source: str = "unknown"
    quote_span_s: float = 0.0
    built_at: float | None = None     # epoch seconds of the newest leg
    carry_rate: float | None = None   # annualised carry from put-call parity; None -> RISK_FREE

    @property
    def options_on_futures(self) -> bool:
        return (self.exchange or "").strip().upper() in _OPTIONS_ON_FUTURES

    @property
    def r(self) -> float:
        return RISK_FREE if self.carry_rate is None else self.carry_rate

    @property
    def _asof_date(self) -> dt.date:
        if self.asof is None:
            return ist_now().date()
        return self.asof.date() if isinstance(self.asof, dt.datetime) else self.asof

    @property
    def days_to_expiry(self) -> int:
        return max(0, (self.expiry - self._asof_date).days)

    @property
    def expired(self) -> bool:
        return self._asof_date > self.expiry

    @property
    def t_years(self) -> float:
        """Year fraction to expiry. Intraday-aware: on expiry day it decays with
        the clock toward the 15:30 close rather than sitting at a half-day floor,
        because same-day theta on a 0-DTE option is the whole game.

        After expiry: 0 (intrinsic only). Before expiry day: calendar days/365
        with a half-day floor. On expiry day: remaining trading hours/(6.25h)
        scaled into a day, floored so greeks never collapse to zero mid-session.
        """
        if self.expired:
            return 0.0
        dte = self.days_to_expiry
        if dte > 0:
            return dte / 365.0
        # expiry is today — decay intraday toward the close
        now = self.asof if isinstance(self.asof, dt.datetime) else ist_now()
        secs_left = (dt.datetime.combine(self._asof_date, MARKET_CLOSE, IST)
                     - _as_ist(now)).total_seconds()
        frac_day = max(secs_left, 600.0) / (6.25 * 3600.0)   # >=10 min floor
        return max(frac_day, 0.5) / 365.0

    @property
    def strikes(self) -> list[float]:
        return sorted({q.strike for q in self.quotes})

    @property
    def strike_step(self) -> float:
        ks = self.strikes
        if len(ks) < 2:
            return max(1.0, round(self.spot * 0.01))
        diffs = sorted(round(b - a, 4) for a, b in zip(ks, ks[1:]))
        return diffs[len(diffs) // 2] or 1.0

    @property
    def atm(self) -> float:
        return min(self.strikes, key=lambda k: abs(k - self.spot)) if self.strikes else self.spot

    def get(self, strike: float, is_call: bool) -> OptionQuote | None:
        for q in self.quotes:
            if abs(q.strike - strike) < 1e-6 and q.is_call == is_call:
                return q
        return None

    def calls(self) -> list[OptionQuote]:
        return sorted([q for q in self.quotes if q.is_call], key=lambda q: q.strike)

    def puts(self) -> list[OptionQuote]:
        return sorted([q for q in self.quotes if not q.is_call], key=lambda q: q.strike)

    def implied_forward(self, band: float = 0.10, min_estimates: int = 3):
        """(forward, n) implied by this chain's own put-call parity (median of
        near-the-money C-P+K). Returns (None, n) when too few strikes qualify."""
        if self.spot <= 0:
            return None, 0
        ests = []
        for k in self.strikes:
            if abs(k - self.spot) > band * self.spot:
                continue
            c, p = self.get(k, True), self.get(k, False)
            if not (c and p) or c.mid <= 0.05 or p.mid <= 0.05:
                continue
            ests.append(c.mid - p.mid + k)
        if len(ests) < min_estimates:
            return None, len(ests)
        ests.sort()
        return ests[len(ests) // 2], len(ests)

    def liquid(self, min_oi: int = 100, max_spread_pct: float = 25.0,
               min_price: float = 0.5, require_book: bool = True) -> list[OptionQuote]:
        """Contracts a retail order can actually get filled in."""
        out = []
        for q in self.quotes:
            if q.oi < min_oi or q.mid < min_price:
                continue
            if require_book and not (q.bid > 0 and q.ask > 0):
                continue
            if q.spread_pct and q.spread_pct > max_spread_pct:
                continue
            out.append(q)
        return out


# ── NSE calendar / session helpers (IST) ────────────────────────────────────
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)


def _as_ist(t: dt.datetime) -> dt.datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(IST)


def ist_now(now: dt.datetime | None = None) -> dt.datetime:
    return _as_ist(now or dt.datetime.now(dt.timezone.utc))


def ist_today(now: dt.datetime | None = None) -> dt.date:
    """The date on the NSE calendar, regardless of the host clock (UTC servers
    lag the Indian trading day between 18:30 UTC and IST midnight)."""
    return ist_now(now).date()


def market_session(now: dt.datetime | None = None, *, holidays=None) -> dict:
    """Is the NSE cash/F&O session live right now, and if not, WHY not."""
    ist = ist_now(now)
    day, clock = ist.date(), ist.time()
    out = {"ist": ist.strftime("%Y-%m-%d %H:%M:%S"), "open": False,
           "session": f"{MARKET_OPEN:%H:%M}–{MARKET_CLOSE:%H:%M} IST"}
    if ist.weekday() >= 5:
        out["reason"] = "it is " + ist.strftime("%A") + " — NSE trades Monday to Friday"
        return out
    if holidays and day.isoformat() in {str(h) for h in holidays}:
        out["reason"] = f"{day.isoformat()} is an NSE holiday"
        return out
    if clock < MARKET_OPEN:
        out["reason"] = f"pre-open — it is {clock:%H:%M} IST, NSE opens {MARKET_OPEN:%H:%M}"
        return out
    if clock > MARKET_CLOSE:
        out["reason"] = f"closed — it is {clock:%H:%M} IST, NSE closed {MARKET_CLOSE:%H:%M}"
        return out
    out["open"] = True
    out["reason"] = f"the session is live ({clock:%H:%M} IST)"
    return out
