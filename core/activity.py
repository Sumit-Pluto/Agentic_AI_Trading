"""ActivityLog — in-memory, thread-safe activity journal for the UI.

A single module-level ``activity`` singleton collects short human-readable
events ({time, kind, text, ...extras}) from the scanner, exit engine,
orchestrator and kill switch.  Bounded (deque maxlen), lock-guarded, and
guaranteed to NEVER raise — instrumentation must never take the pipeline
down.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

_RESERVED = ("time", "kind", "text")


class ActivityLog:
    """Bounded, thread-safe, newest-last event journal."""

    def __init__(self, maxlen: int = 300):
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=maxlen)

    def add(self, kind, text, **extra) -> None:
        """Append {time iso, kind, text, ...extra}.  Swallows everything."""
        try:
            item = {"time": datetime.now().isoformat(timespec="seconds"),
                    "kind": str(kind), "text": str(text)}
            for k, v in extra.items():
                if k in _RESERVED:
                    continue                    # never clobber core fields
                try:
                    item[str(k)] = (v if isinstance(
                        v, (str, int, float, bool, type(None))) else str(v))
                except Exception:
                    continue
            with self._lock:
                self._items.append(item)
        except Exception:
            pass                                # journal must never raise

    def recent(self, n: int = 100) -> list:
        """Last ``n`` events, NEWEST FIRST.  Never raises."""
        try:
            with self._lock:
                items = list(self._items)
            if n is None or n <= 0:
                return []
            return [dict(x) for x in reversed(items[-int(n):])]
        except Exception:
            return []

    def __len__(self) -> int:
        try:
            with self._lock:
                return len(self._items)
        except Exception:
            return 0


# module-level singleton — everyone imports this
activity = ActivityLog()


# ── self-test (offline) ──────────────────────────────────────────────────
if __name__ == "__main__":
    al = ActivityLog(maxlen=300)
    for i in range(350):
        al.add("test", f"event {i}", seq=i)
    assert len(al) == 300, len(al)
    rec = al.recent(400)
    assert len(rec) == 300 and rec[0]["seq"] == 349 and rec[-1]["seq"] == 50
    assert len(al.recent()) == 100 and al.recent(5)[0]["text"] == "event 349"
    assert al.recent(0) == []
    # never raises: weird kinds, unserializable extras, reserved keys
    al.add(None, object(), time="clobber?", payload=object(),
           df=[1, 2, 3])
    top = al.recent(1)[0]
    assert top["kind"] == "None" and "T" in top["time"], top
    assert top["df"] == "[1, 2, 3]"
    # threaded hammering
    import threading as _t
    ts = [_t.Thread(target=lambda: [al.add("thr", i) for i in range(200)])
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(al) == 300
    assert isinstance(activity, ActivityLog)
    print("activity self-test OK:", len(al), "items,",
          al.recent(1)[0]["kind"])
