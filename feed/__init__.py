"""feed/ — push-based Shoonya tick layer.

Adopts the Snowball system's consumption model (a freshness-aware O(1) tick
cache + event-driven handling) on top of this project's existing proxy-aware
websocket transport (`shoonya_client.ShoonyaFeed`). See
docs/CPP_ENGINE_AND_FEED_ARCHITECTURE.md.
"""
from __future__ import annotations

from feed.cache import PriceState, TickCache

__all__ = ["PriceState", "TickCache"]
