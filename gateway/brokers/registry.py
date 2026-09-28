"""Broker registry — maps broker names to adapter instances."""

from brokers.base import BrokerBase
from brokers.shoonya import ShoonyaBroker

BROKER_REGISTRY: dict[str, BrokerBase] = {
    "shoonya": ShoonyaBroker(),
}

# Sharekhan is optional: this vendored Gateway targets Shoonya (Finvasia) only.
# Its adapter pulls in the `shareconnect` SDK, which need not be installed for
# an intraday-only deployment. Register it lazily and never fail the whole
# gateway if the dependency is absent.
try:  # pragma: no cover - exercised only where shareconnect is installed
    from brokers.sharekhan import SharekhanBroker

    BROKER_REGISTRY["sharekhan"] = SharekhanBroker()
except Exception:  # ImportError (no shareconnect) or adapter init failure
    pass


def get_broker(broker_name: str) -> BrokerBase:
    """
    Look up a broker adapter by name.

    Args:
        broker_name: Broker name (case-insensitive), e.g. "shoonya", "sharekhan"

    Returns:
        BrokerBase instance

    Raises:
        KeyError: If broker_name is not registered
    """
    try:
        return BROKER_REGISTRY[broker_name.lower()]
    except KeyError:
        available = list(BROKER_REGISTRY.keys())
        raise KeyError(
            f"No broker adapter registered for '{broker_name}'. "
            f"Available: {available}"
        )
