"""Order Management Agent (PDF §2.2) — the single execution choke point.

Every order (entry or exit) flows through submit(): local rule check → place
ONCE → latency-stamp (§7 <5s gate) → journal the outcome. Placement is never
retried — a re-fired PlaceOrder can double-fill (the Gateway/adapter own
idempotency; transient retries are for idempotent reads only). A placement that
misses the <5s gate is TAGGED late, not cancelled, exactly as swing does.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

LATENCY_GATE_MS = 5000.0        # PDF §7


@dataclass
class SubmitResult:
    status: str                 # FILLED | OK | BLOCKED | REJECTED | TIMEOUT | WORKING | ERROR
    order_id: int | None = None
    broker_order_id: str = ""
    fill_px: float | None = None
    filled_qty: int | None = None   # shares actually filled (live partials)
    latency_ms: float | None = None
    late: bool = False
    reason: str = ""
    ack: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status in ("FILLED", "OK")


class OrderManager:
    def __init__(self, broker, store, rules, cfg: dict | None = None):
        self.broker = broker
        self.store = store
        self.rules = rules
        self.cfg = cfg or {}

    def submit(self, intent, now: dt.datetime, open_positions: list, *,
               is_exit: bool = False, halted: bool = False,
               paused: bool = False) -> SubmitResult:
        # 1. local rule gate (fast, no round trip). The Gateway coordinator is
        #    the authoritative cross-strategy backstop for §8.
        ok, reason = self.rules.check(intent, now, open_positions, is_exit=is_exit,
                                      halted=halted, paused=paused)
        if not ok:
            self.store.save_order(intent, status=f"BLOCKED:{reason}"[:60],
                                  broker=self.broker.name, date=now.date())
            return SubmitResult(status="BLOCKED", reason=reason)

        # 2. latency stamp — signal_ts is set when the signal fired (wall clock)
        latency_ms = None
        if intent.signal_ts:
            latency_ms = max(0.0, (time.time() - intent.signal_ts) * 1000.0)
        late = latency_ms is not None and latency_ms > LATENCY_GATE_MS

        # 3. place ONCE (never retry a placement)
        try:
            ack = self.broker.place(intent)
        except Exception as e:
            self.store.save_order(intent, status="ERROR", broker=self.broker.name,
                                  date=now.date(), latency_ms=latency_ms)
            return SubmitResult(status="ERROR", latency_ms=latency_ms, late=late,
                                reason=f"{type(e).__name__}: {e}")

        status = str(ack.get("status", "OK")).upper()
        boid = str(ack.get("broker_order_id", ""))
        fill_px = ack.get("fill_px")
        try:
            filled_qty = int(float(ack.get("filled_qty"))) if ack.get("filled_qty") else None
        except (TypeError, ValueError):
            filled_qty = None
        oid = self.store.save_order(intent, status=status, broker=self.broker.name,
                                    date=now.date(), broker_order_id=boid,
                                    latency_ms=latency_ms)
        if status == "FILLED" and fill_px:
            self.store.fill_order(oid, float(fill_px))
        if status in ("TIMEOUT", "WORKING"):
            # A live order left working at the broker is journalled LOUDLY: the
            # engine tracks nothing for it, so a human must resolve the book.
            self.store.set_order_status(oid, f"{status}:{boid or 'no-id'}"[:60])
        return SubmitResult(status=status, order_id=oid, broker_order_id=boid,
                            fill_px=(float(fill_px) if fill_px else None),
                            filled_qty=filled_qty,
                            latency_ms=latency_ms, late=late,
                            reason=str(ack.get("reason") or ("latency>5s" if late else "ok"))[:120],
                            ack=ack)
