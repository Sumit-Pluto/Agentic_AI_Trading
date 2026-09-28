"""Compatibility shim — the real FastAPI app lives in app.main.

Existing invocations like `uvicorn server:app` keep working. New code should
import from `app.main` directly.
"""
from app.main import app  # noqa: F401
