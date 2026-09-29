"""Phase 3 backend — FastAPI serving the cockpit: REST from the journal + a
WebSocket that pushes the live engine state each tick. Runs the SessionLoop
against SimContext (demo, no broker) or LiveContext (Gateway) on a background
thread. See app.py / runner.py.
"""
from .runner import EngineRunner

__all__ = ["EngineRunner"]
