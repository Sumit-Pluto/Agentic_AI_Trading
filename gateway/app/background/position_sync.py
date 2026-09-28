import asyncio

from app.core import auth as _auth_module
from app.services.reconciliation import _sync_positions_to_lots
from app.services.targets import _load_target_positions


async def _sync_positions_to_lots_on_startup() -> None:
    """Deferred position sync — waits up to 30s for the user to log in after startup."""
    for _ in range(30):
        if _auth_module._auth is not None:
            break
        await asyncio.sleep(1)
    if _auth_module._auth is None:
        return
    await _sync_positions_to_lots(_auth_module._auth.user_id)
    await _load_target_positions()
