"""File-sentinel trade mode: PAPER (default) vs LIVE (real money).

Same pattern as core.killswitch — the mode is a FILE (state/live_mode.flag)
so it survives process restarts.  That persistence is deliberate and
safety-critical: if the process crashes mid-day with real positions open,
the restart MUST come back in live mode so the exit engine keeps routing
stops / targets / EOD square-offs to the broker instead of silently
downgrading them to paper rows.

Paper is the default: no file -> is_live() is False.  Going live always
requires an explicit set_live(True) (UI button with typed confirmation,
or POST /api/mode).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("trade_mode")

FLAG_PATH = Path(__file__).resolve().parent.parent / "state" / "live_mode.flag"


def is_live() -> bool:
    """True iff the sentinel file exists.  Never raises."""
    try:
        return FLAG_PATH.exists()
    except Exception:
        return False


def mode() -> str:
    return "live" if is_live() else "paper"


def set_live(live: bool, source: str = "ui") -> bool:
    """Create/remove the sentinel.  Returns the resulting is_live() state."""
    try:
        if live:
            FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            FLAG_PATH.write_text(
                f"LIVE since {datetime.now().isoformat(timespec='seconds')} "
                f"by {source}\n")
        else:
            try:
                FLAG_PATH.unlink()
            except FileNotFoundError:
                pass
        try:
            from core.activity import activity
            activity.add("trade_mode",
                         f"{'LIVE trading ENABLED' if live else 'back to PAPER'}"
                         f" by {source}",
                         source=source, live=bool(live))
        except Exception:
            pass
        log.warning("trade mode %s by %s",
                    "LIVE (real money!)" if live else "PAPER", source)
    except Exception as e:
        log.warning("trade_mode set_live(%s) failed: %s", live, e)
    return is_live()


# ── self-test (offline; uses a temp sentinel, real flag untouched) ──────
if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    _real = FLAG_PATH
    FLAG_PATH = _P(tempfile.mkdtemp(prefix="tm_selftest_")) / "s" / "live.flag"

    assert is_live() is False and mode() == "paper"     # default = paper
    assert set_live(True, source="test") is True
    assert is_live() is True and mode() == "live"
    assert FLAG_PATH.read_text().startswith("LIVE since ")
    assert FLAG_PATH.exists() and is_live() is True     # survives "restart"
    assert set_live(True, source="test") is True        # idempotent
    assert set_live(False, source="test") is False
    assert is_live() is False and not FLAG_PATH.exists()
    assert set_live(False, source="test") is False      # idempotent clear
    FLAG_PATH = _real
    print("trade_mode self-test OK (default paper + round-trip)")
