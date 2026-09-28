"""ExecutionCoordinator (§8): per-contract order-lock, cross-strategy intent
dedup, and per-account serialized submit."""
import asyncio

import pytest

from app.services.execution_coordinator import (ExecutionConflict,
                                                ExecutionCoordinator)


def test_reserve_blocks_inflight_same_contract():
    c = ExecutionCoordinator()
    ok, _ = c.reserve("U1", "NFO", "NIFTY24800CE", "B", 50, 120.0)
    assert ok
    # a second order on the SAME contract while the first is in flight → blocked
    ok2, why = c.reserve("U1", "NFO", "NIFTY24800CE", "S", 50, 90.0)
    assert ok2 is False and "in flight" in why
    # a different contract is fine
    ok3, _ = c.reserve("U1", "NFO", "NIFTY24900CE", "B", 50, 80.0)
    assert ok3


def test_reserve_dedups_identical_intent_after_release():
    c = ExecutionCoordinator(dedup_ttl=60.0)
    ok, _ = c.reserve("U1", "NFO", "BANKNIFTY52000PE", "B", 15, 200.0)
    assert ok
    c.release("U1", "NFO", "BANKNIFTY52000PE")     # contract no longer in flight
    # identical intent within the dedup TTL is still refused (idempotency)
    ok2, why = c.reserve("U1", "NFO", "BANKNIFTY52000PE", "B", 15, 200.0)
    assert ok2 is False and "duplicate" in why
    # a different size is a different intent → allowed
    ok3, _ = c.reserve("U1", "NFO", "BANKNIFTY52000PE", "B", 30, 200.0)
    assert ok3


def test_different_accounts_do_not_collide():
    c = ExecutionCoordinator()
    assert c.reserve("U1", "NFO", "NIFTY24800CE", "B", 50, 120.0)[0]
    assert c.reserve("U2", "NFO", "NIFTY24800CE", "B", 50, 120.0)[0]   # other account ok


def test_submit_serializes_and_rejects_concurrent_duplicate():
    """Two coroutines racing the same contract: exactly one places, the other
    is refused with ExecutionConflict (the race §8 must prevent)."""
    c = ExecutionCoordinator(dedup_ttl=60.0)
    placed = []

    async def place():
        await asyncio.sleep(0.01)
        placed.append(1)
        return {"status": "OK"}

    async def one():
        try:
            return await c.submit("U1", "NFO", "NIFTY24800CE", "B", 50, 120.0, place)
        except ExecutionConflict:
            return "conflict"

    async def main():
        return await asyncio.gather(one(), one())

    results = asyncio.run(main())
    assert placed == [1]                       # placed exactly once
    assert "conflict" in results               # the other was refused
