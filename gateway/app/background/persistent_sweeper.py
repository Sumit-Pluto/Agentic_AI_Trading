import asyncio
import logging

from app.services.persistent_orders import _sweep_persistent_orders

logger = logging.getLogger(__name__)


async def _persistent_sweeper_loop() -> None:
    """Long-running background task. Started once on backend startup."""
    while True:
        try:
            await _sweep_persistent_orders()
        except Exception:
            logger.exception("Persistent sweeper iteration failed")
        await asyncio.sleep(60)
