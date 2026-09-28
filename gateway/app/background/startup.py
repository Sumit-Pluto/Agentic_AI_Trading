"""Startup wiring: register ticker callbacks and launch long-running tasks.

Called from the FastAPI `@app.on_event("startup")` handler in `app.main`.
Order preserved from the original monolithic server.py.
"""
import asyncio

from app.background.persistent_sweeper import _persistent_sweeper_loop
from app.background.position_sync import _sync_positions_to_lots_on_startup
from app.services.reconciliation import _on_order_update_event
from app.services.targets import _on_target_tick
from ticker_manager import ticker_manager


def register_ticker_callbacks() -> None:
    ticker_manager.on_tick_callback = _on_target_tick
    ticker_manager.on_order_update_callback = _on_order_update_event


def launch_tasks() -> None:
    asyncio.create_task(_persistent_sweeper_loop())
    asyncio.create_task(_sync_positions_to_lots_on_startup())
