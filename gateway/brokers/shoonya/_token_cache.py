"""Multi-account token cache for Shoonya sessions. Replaces token_store.json in phases."""

import json
from pathlib import Path
from datetime import datetime, date
from typing import Optional


_TOKEN_STORE_PATH = Path(__file__).parent.parent.parent / "token_store.json"


def _ensure_path():
    """Create token_store.json if it doesn't exist."""
    if not _TOKEN_STORE_PATH.exists():
        _TOKEN_STORE_PATH.write_text(json.dumps({}))


def load() -> dict:
    """
    Load and migrate token_store.json.

    Old format (flat): {"susertoken": "...", "susertoken_issued_at": "...", "user_id": "..."}
    New format (keyed): {"FN131640": {"susertoken": "...", "susertoken_issued_at": "...", ...}}

    Detects old format and auto-migrates on first read.
    """
    _ensure_path()
    try:
        data = json.loads(_TOKEN_STORE_PATH.read_text())
    except (json.JSONDecodeError, IOError):
        return {}

    if not data:
        return {}

    # Detect old flat format: has "susertoken" key at top level and "user_id" as string
    if "susertoken" in data and isinstance(data.get("user_id"), str):
        user_id = data.pop("user_id")
        migrated = {user_id: data}
        save(migrated)
        return migrated

    return data


def save(store: dict) -> None:
    """Write token store to disk."""
    _ensure_path()
    _TOKEN_STORE_PATH.write_text(json.dumps(store, indent=2))


def _issued_today(timestamp_str: Optional[str]) -> bool:
    """Check if a timestamp string (ISO format) is from today."""
    if not timestamp_str:
        return False
    try:
        issued_date = datetime.fromisoformat(timestamp_str).date()
        return issued_date == date.today()
    except (ValueError, AttributeError):
        return False


def get_cached_session(user_id: str) -> Optional[dict]:
    """
    Return cached session entry for user_id if susertoken was issued today, else None.
    Entry shape: {"susertoken": str, "susertoken_issued_at": str, ...}
    """
    store = load()
    entry = store.get(user_id, {})

    if entry.get("susertoken") and _issued_today(entry.get("susertoken_issued_at")):
        return entry
    return None


def cache_session(user_id: str, token: str, issued_at: str) -> None:
    """Write or update the session token entry for user_id."""
    store = load()
    if user_id not in store:
        store[user_id] = {}
    store[user_id]["susertoken"] = token
    store[user_id]["susertoken_issued_at"] = issued_at
    save(store)


def invalidate_session(user_id: str) -> None:
    """Remove the session token entry for user_id."""
    store = load()
    if user_id in store:
        store[user_id].pop("susertoken", None)
        store[user_id].pop("susertoken_issued_at", None)
        if not store[user_id]:
            store.pop(user_id)
    save(store)


def get_cached_oauth_code(user_id: str) -> Optional[str]:
    """Return today's OAuth code for user_id if cached, else None."""
    store = load()
    entry = store.get(user_id, {})

    if entry.get("oauth_code") and _issued_today(entry.get("oauth_code_issued_at")):
        return entry["oauth_code"]
    return None


def cache_oauth_code(user_id: str, code: str) -> None:
    """Cache an OAuth code for user_id with today's timestamp."""
    store = load()
    if user_id not in store:
        store[user_id] = {}
    store[user_id]["oauth_code"] = code
    store[user_id]["oauth_code_issued_at"] = datetime.now().isoformat()
    save(store)


def invalidate_oauth_code(user_id: str) -> None:
    """Remove the OAuth code entry for user_id."""
    store = load()
    if user_id in store:
        store[user_id].pop("oauth_code", None)
        store[user_id].pop("oauth_code_issued_at", None)
        if not store[user_id]:
            store.pop(user_id)
    save(store)
