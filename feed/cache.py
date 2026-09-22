"""feed/cache.py — O(1) freshness-aware tick cache (Snowball PriceState model).

Adapted from the Snowball system's ``core/engine.py`` ``PriceState`` / ``_on_tick``,
decoupled from any DB or strategy. It sits on top of this project's proxy-aware
``shoonya_client.ShoonyaFeed`` transport: the feed's ``on_tick(msg, snap)``
callback pushes merged Shoonya touchline/depth frames into a ``TickCache``, which
the scanner / DataHub read by ``"EXCH|TOKEN"`` key.

Why this instead of reading ``ShoonyaFeed.quotes`` directly:

* **Freshness.** Every write stamps a *monotonic* timestamp, so a reader can ask
  "is this price fresh?" (``is_fresh``/``age``) — immune to wall-clock jumps
  (NTP steps, DST). Raw ``feed.quotes`` dicts have no notion of staleness.
* **Typed O(1) access.** Slotted floats instead of string-valued dicts, so the
  hot path doesn't re-parse ``float(...)`` on every read.
* **Partial-frame safe.** Shoonya ``tf``/``df`` frames carry only the *changed*
  fields; ``apply`` merges rather than overwrites, under a lock so a reader never
  sees a half-applied frame.

This module is self-contained and unit-testable offline — no network, no broker,
no project imports. Run ``python -m feed.cache`` for a built-in smoke test.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

# IST, for the cosmetic wall-clock stamp only. Freshness never depends on it.
_IST = timezone(timedelta(hours=5, minutes=30))

# Shoonya numeric field name -> PriceState slot.
_NUM_FIELDS: tuple[tuple[str, str], ...] = (
    ("lp", "lp"),     # last traded price
    ("bp1", "bid"),   # best bid
    ("sp1", "ask"),   # best ask
    ("o", "open"),
    ("h", "high"),
    ("l", "low"),
    ("c", "close"),
    ("v", "volume"),
    ("oi", "oi"),     # open interest
)


def _num(v) -> Optional[float]:
    """Parse a Shoonya numeric field (they arrive as strings). None if absent/blank."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class PriceState:
    """Latest known state for one instrument. Slotted for size and speed.

    Freshness is measured with a monotonic clock: ``age()`` is the seconds since
    the last update, and a large sentinel (``1e9``) means "never updated".
    """

    __slots__ = ("lp", "bid", "ask", "open", "high", "low", "close",
                 "volume", "oi", "ft", "wall_ts", "mono_ts", "source")

    def __init__(self) -> None:
        self.lp = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume = 0.0
        self.oi = 0.0
        self.ft = ""            # Shoonya feed time (epoch secs, as sent)
        self.wall_ts = ""       # human-readable IST stamp (cosmetic)
        self.mono_ts = 0.0      # monotonic stamp — the source of truth for age
        self.source = ""        # "ws" | "poll" | ...

    def age(self) -> float:
        """Seconds since last update; ``1e9`` if never updated."""
        return 1e9 if self.mono_ts == 0.0 else _time.monotonic() - self.mono_ts

    def is_fresh(self, max_age: float) -> bool:
        """True if updated within the last ``max_age`` seconds."""
        return self.mono_ts != 0.0 and (_time.monotonic() - self.mono_ts) <= max_age

    def as_dict(self) -> dict:
        return {
            "lp": self.lp, "bid": self.bid, "ask": self.ask,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "volume": self.volume, "oi": self.oi,
            "ft": self.ft, "ts": self.wall_ts,
            "age": round(min(self.age(), 9999.0), 1), "source": self.source,
        }

    def as_quote(self) -> dict:
        """Shoonya-GetQuotes-shaped dict, so a cached tick can transparently
        stand in for a REST quote to existing DataHub consumers (which read
        ``lp``/``oi``/``v``/``bp1``/``sp1``)."""
        return {"lp": self.lp, "oi": self.oi, "v": self.volume,
                "bp1": self.bid, "sp1": self.ask}


class TickCache:
    """Thread-safe O(1) map of ``"EXCH|TOKEN"`` -> :class:`PriceState`.

    Single-writer (the websocket dispatch thread) / many-reader (scanner,
    DataHub, UI). Multi-field writes are guarded by a lock so a reader taking a
    :meth:`snapshot` never sees a half-applied frame; scalar getters
    (:meth:`lp`) are lock-free (a single float read is atomic under the GIL).
    The lock is uncontended in practice — a few hundred instruments, sub-µs
    critical sections.
    """

    def __init__(self) -> None:
        self._d: dict[str, PriceState] = {}
        self._lock = threading.Lock()

    # -- writes -------------------------------------------------------------

    def apply(self, msg: dict, source: str = "ws") -> Optional[str]:
        """Merge one Shoonya touchline/depth frame (``tk``/``tf``/``dk``/``df``).

        Only fields present in ``msg`` overwrite the cache. Returns the affected
        ``"EXCH|TOKEN"`` key, or ``None`` if the frame lacks exch/token.
        """
        exch = msg.get("e")
        tok = msg.get("tk")
        if not exch or not tok:
            return None
        key = f"{exch}|{tok}"
        now = _time.monotonic()
        wall = datetime.now(_IST).isoformat(timespec="seconds")
        with self._lock:
            ps = self._d.get(key)
            if ps is None:
                ps = PriceState()
                self._d[key] = ps
            for field, slot in _NUM_FIELDS:
                val = _num(msg.get(field))
                if val is not None:
                    setattr(ps, slot, val)
            ft = msg.get("ft")
            if ft:
                ps.ft = str(ft)
            ps.wall_ts = wall
            ps.mono_ts = now
            ps.source = source
        return key

    def apply_quote(self, key: str, *, lp: float = 0.0, bid: float = 0.0,
                    ask: float = 0.0, source: str = "poll") -> None:
        """Write a REST-polled quote back into the cache (stale-fallback path).

        Mirrors Snowball's ``_price_keeper_loop`` writeback: when the websocket
        goes quiet for a token, a batched REST poll refreshes it here with
        ``source="poll"`` so freshness keeps advancing.
        """
        now = _time.monotonic()
        wall = datetime.now(_IST).isoformat(timespec="seconds")
        with self._lock:
            ps = self._d.get(key)
            if ps is None:
                ps = PriceState()
                self._d[key] = ps
            if lp:
                ps.lp = lp
            if bid:
                ps.bid = bid
            if ask:
                ps.ask = ask
            ps.wall_ts = wall
            ps.mono_ts = now
            ps.source = source

    # -- reads --------------------------------------------------------------

    def get(self, key: str) -> Optional[PriceState]:
        """The live :class:`PriceState` for ``key`` (or ``None``). Not a copy."""
        return self._d.get(key)

    def lp(self, key: str, default: float = 0.0) -> float:
        """Last price for ``key`` (lock-free), or ``default`` if unknown."""
        ps = self._d.get(key)
        return ps.lp if ps is not None else default

    def fresh_lp(self, key: str, max_age: float, default: float = 0.0) -> float:
        """Last price only if updated within ``max_age`` seconds, else ``default``."""
        ps = self._d.get(key)
        return ps.lp if (ps is not None and ps.is_fresh(max_age)) else default

    def is_fresh(self, key: str, max_age: float) -> bool:
        ps = self._d.get(key)
        return ps is not None and ps.is_fresh(max_age)

    def snapshot(self, key: str) -> Optional[dict]:
        """Consistent dict copy of one instrument (lock-guarded), or ``None``."""
        with self._lock:
            ps = self._d.get(key)
            return ps.as_dict() if ps is not None else None

    def stale_keys(self, max_age: float, only: Optional[set[str]] = None) -> list[str]:
        """Keys not updated within ``max_age`` seconds — the REST-fallback set.

        If ``only`` is given, restrict to those keys (e.g. the current
        subscription set), including keys never seen yet.
        """
        out: list[str] = []
        if only is not None:
            for key in only:
                ps = self._d.get(key)
                if ps is None or not ps.is_fresh(max_age):
                    out.append(key)
            return out
        for key, ps in list(self._d.items()):
            if not ps.is_fresh(max_age):
                out.append(key)
        return out

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)


if __name__ == "__main__":
    # Offline smoke test — no network. Run: python -m feed.cache
    c = TickCache()

    # Full touchline ack (tk): all fields present.
    k = c.apply({"t": "tk", "e": "NSE", "tk": "26000",
                 "lp": "22150.5", "bp1": "22150.0", "sp1": "22151.0",
                 "o": "22000", "h": "22200", "l": "21980", "c": "22010",
                 "v": "1234567", "oi": "0", "ft": "1720512000"})
    assert k == "NSE|26000"
    ps = c.get(k)
    assert ps.lp == 22150.5 and ps.bid == 22150.0 and ps.ask == 22151.0
    assert ps.high == 22200.0 and ps.source == "ws"

    # Partial feed (tf): only lp changed — everything else must be preserved.
    c.apply({"t": "tf", "e": "NSE", "tk": "26000", "lp": "22160.0"})
    assert ps.lp == 22160.0, "lp should update"
    assert ps.high == 22200.0, "unspecified fields must survive a partial frame"
    assert ps.bid == 22150.0, "bid must survive a partial frame"

    # Freshness.
    assert ps.is_fresh(1.0) and ps.age() < 1.0
    assert c.fresh_lp("NSE|26000", 1.0) == 22160.0

    # Unknown / malformed frames.
    assert c.apply({"t": "tf", "lp": "1"}) is None      # no exch/token
    assert c.lp("NSE|99999") == 0.0                     # unknown key
    assert c.stale_keys(1e-9) == ["NSE|26000"]          # nothing is that fresh

    # REST fallback writeback.
    c.apply_quote("MCX|432556", lp=6820.0, source="poll")
    assert c.get("MCX|432556").source == "poll"
    assert c.lp("MCX|432556") == 6820.0

    print("feed.cache smoke test: OK", f"({len(c)} instruments cached)")
