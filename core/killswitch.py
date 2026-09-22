"""File-sentinel kill switch.

The user's pause is a FILE (state/killswitch.flag) so it survives process
restarts: is_paused() is simply Path.exists().  The orchestrator reconciles
scanner.paused from this flag every tick, so the UI pause button keeps
ruling during market hours while phase-pauses (nights/weekends) never touch
the user's own switch.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("killswitch")

FLAG_PATH = Path(__file__).resolve().parent.parent / "state" / "killswitch.flag"


def is_paused() -> bool:
    """True iff the sentinel file exists.  Never raises."""
    try:
        return FLAG_PATH.exists()
    except Exception:
        return False


def set_paused(paused: bool, source: str = "ui") -> bool:
    """Create/remove the sentinel.  Returns the resulting paused state."""
    try:
        if paused:
            FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            FLAG_PATH.write_text(
                f"paused at {datetime.now().isoformat(timespec='seconds')} "
                f"by {source}\n")
        else:
            try:
                FLAG_PATH.unlink()
            except FileNotFoundError:
                pass
        try:
            from core.activity import activity
            activity.add("killswitch",
                         f"{'PAUSED' if paused else 'RESUMED'} by {source}",
                         source=source, paused=bool(paused))
        except Exception:
            pass
        log.info("killswitch %s by %s",
                 "SET (paused)" if paused else "CLEARED (running)", source)
    except Exception as e:
        log.warning("killswitch set_paused(%s) failed: %s", paused, e)
    return is_paused()


# ── self-test (offline; uses a temp sentinel, real flag untouched) ──────
if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    _real = FLAG_PATH
    tmp = _P(tempfile.mkdtemp(prefix="ks_selftest_"))
    FLAG_PATH = tmp / "nested" / "killswitch.flag"      # parent mkdir path

    assert is_paused() is False
    assert set_paused(True, source="test") is True
    assert is_paused() is True                          # sentinel exists
    assert FLAG_PATH.read_text().startswith("paused at ")
    # "restart": state is purely the file — a fresh reader sees paused
    assert FLAG_PATH.exists() and is_paused() is True
    assert set_paused(True, source="test") is True      # idempotent set
    assert set_paused(False, source="test") is False
    assert is_paused() is False and not FLAG_PATH.exists()
    assert set_paused(False, source="test") is False    # idempotent clear
    try:
        from core.activity import activity
        kinds = [e["kind"] for e in activity.recent(10)]
        assert "killswitch" in kinds, kinds
    except ModuleNotFoundError:
        pass
    FLAG_PATH = _real
    print("killswitch self-test OK (round-trip + idempotency)")
