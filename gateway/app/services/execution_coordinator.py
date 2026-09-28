"""ExecutionCoordinator — Strategy Gateway conflict management (PDF §8).

The Gateway is the only process that sees every strategy client's order flow, so
the authoritative conflict layer lives here (a coordinator inside any one engine
cannot see the others). It provides the §8 primitives:

  • order-lock per (account, contract): no two in-flight orders on the same leg
  • cross-strategy intent dedup: an identical (account, contract, side, qty,
    price-bucket) order seen within a short TTL is refused (idempotency)
  • per-account serialization: submit() runs placements one-at-a-time per account
    (kills the race, respects broker rate limits)
  • exposure hook: an optional per-account cap the caller can enforce

`reserve()` is the lightweight, synchronous guard wired into POST /api/orders — it
is atomic on the asyncio loop (no await between check and record) and the
in-flight/dedup entries auto-expire, so no explicit release is needed on the hot
path. `submit()` is the fuller async form (per-account lock + reserve + release)
used where the caller drives the placement coroutine.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field


class ExecutionConflict(RuntimeError):
    """A submission was refused by the coordinator (duplicate / in-flight / cap)."""


def _now() -> float:
    return time.monotonic()


@dataclass
class ExecutionCoordinator:
    dedup_ttl: float = 3.0            # identical intent refused within this window
    inflight_ttl: float = 8.0         # a contract is "in flight" at most this long
    price_bucket: float = 0.5         # price granularity for the dedup key
    _recent: dict = field(default_factory=dict)     # intent_key -> ts
    _inflight: dict = field(default_factory=dict)    # (uid,exch,tsym) -> ts
    _acct_locks: dict = field(default_factory=dict)  # uid -> asyncio.Lock
    _guard: threading.Lock = field(default_factory=threading.Lock)

    # ---------- keys / pruning ----------
    def _intent_key(self, uid, exch, tsym, side, qty, price):
        bucket = round(float(price or 0) / self.price_bucket) if self.price_bucket else 0
        return (uid, exch, str(tsym).upper(), str(side).upper()[:1], int(qty), bucket)

    def _prune(self, now: float):
        for k in [k for k, ts in self._recent.items() if now - ts > self.dedup_ttl]:
            self._recent.pop(k, None)
        for k in [k for k, ts in self._inflight.items() if now - ts > self.inflight_ttl]:
            self._inflight.pop(k, None)

    # ---------- synchronous guard (hot path) ----------
    def reserve(self, uid: str, exch: str, tsym: str, side: str, qty: int,
                price: float = 0.0) -> tuple[bool, str]:
        """Atomically check + record. Returns (ok, reason). Safe to call from an
        async handler: it does no awaiting, so nothing interleaves between the
        check and the record on the event loop. A threading.Lock also guards the
        rare case of a threadpool caller."""
        with self._guard:
            now = _now()
            self._prune(now)
            contract = (uid, exch, str(tsym).upper())
            if contract in self._inflight:
                return False, f"an order on {tsym} is already in flight for this account"
            key = self._intent_key(uid, exch, tsym, side, qty, price)
            if key in self._recent:
                return False, (f"duplicate order refused: identical {side} {qty} {tsym} "
                               f"within {self.dedup_ttl:g}s")
            self._recent[key] = now
            self._inflight[contract] = now
            return True, "ok"

    def release(self, uid: str, exch: str, tsym: str) -> None:
        with self._guard:
            self._inflight.pop((uid, exch, str(tsym).upper()), None)

    # ---------- async serialized submit (per account) ----------
    def _acct_lock(self, uid: str) -> asyncio.Lock:
        with self._guard:
            lock = self._acct_locks.get(uid)
            if lock is None:
                lock = asyncio.Lock()
                self._acct_locks[uid] = lock
            return lock

    async def submit(self, uid: str, exch: str, tsym: str, side: str, qty: int,
                     price: float, place_coro):
        """Serialize per account: acquire the account lock, reserve the contract,
        run the placement coroutine, then release. Raises ExecutionConflict if
        the reservation is refused."""
        async with self._acct_lock(uid):
            ok, reason = self.reserve(uid, exch, tsym, side, qty, price)
            if not ok:
                raise ExecutionConflict(reason)
            try:
                return await place_coro()
            finally:
                self.release(uid, exch, tsym)


# module-level singleton shared by the Gateway
coordinator = ExecutionCoordinator()
