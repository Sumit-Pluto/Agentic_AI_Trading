"""Rule Engine (PDF §2.3) — pre-trade compliance gate (strategy-local first pass).

The authoritative cross-strategy backstop is the Gateway ExecutionCoordinator
(§8); this is the fast, local check that fails without a round trip.
"""
from .engine import RuleEngine

__all__ = ["RuleEngine"]
