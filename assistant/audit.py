"""assistant/audit.py — per-turn audit log (provenance) with a ring buffer.

Guardrail #2: every interaction is logged with WHICH tool calls fed the reply,
so if the user disputes advice you can show provenance. Tool *results* are
hashed (digest), not stored raw, to keep the file small and secret-free — the
live state files are the source of truth if a full replay is ever needed.

Retention: the most recent 100 conversations (by session_id); older sessions
are pruned. One JSON record per line at state/assistant_audit.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone, timedelta

AUDIT_PATH = os.path.join("state", "assistant_audit.jsonl")
MAX_CONVERSATIONS = 100
_IST = timezone(timedelta(hours=5, minutes=30))


def _digest(obj) -> str:
    try:
        blob = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        blob = str(obj)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: str = AUDIT_PATH,
                 max_conversations: int = MAX_CONVERSATIONS):
        self.path = path
        self.max = max_conversations
        self._lock = threading.Lock()

    def record(self, *, session_id: str, user_msg: str, tool_calls: list,
               reply: str, mode: str, validator: str) -> dict:
        rec = {
            "ts": datetime.now(_IST).isoformat(timespec="seconds"),
            "session_id": session_id, "user_msg": user_msg,
            "tool_calls": [{"name": c.get("name"), "args": c.get("args"),
                            "result_digest": _digest(c.get("result"))}
                           for c in tool_calls],
            "reply": reply, "mode": mode, "validator": validator,
        }
        with self._lock:
            self._append(rec)
            self._prune()
        return rec

    def _append(self, rec: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def _read_all(self) -> list[dict]:
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            pass
        return out

    def _prune(self) -> None:
        recs = self._read_all()
        # newest-100 sessions by their most-recent record
        last_seen: dict[str, int] = {}
        for i, r in enumerate(recs):
            last_seen[r.get("session_id", "")] = i
        if len(last_seen) <= self.max:
            return
        keep_sessions = set(sorted(last_seen, key=lambda s: last_seen[s])[-self.max:])
        kept = [r for r in recs if r.get("session_id", "") in keep_sessions]
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                for r in kept:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def tail(self, n: int = 20) -> list[dict]:
        return self._read_all()[-n:]


if __name__ == "__main__":
    import tempfile
    AUDIT_PATH = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
    log = AuditLog(AUDIT_PATH, max_conversations=2)
    log.record(session_id="s1", user_msg="pnl?", reply="...", mode="explanation",
               validator="pass", tool_calls=[{"name": "get_pnl", "args": {},
                                              "result": {"realized": 1234.5}}])
    log.record(session_id="s2", user_msg="hi", reply="hello", mode="explanation",
               validator="pass", tool_calls=[])
    log.record(session_id="s3", user_msg="x", reply="y", mode="explanation",
               validator="pass", tool_calls=[])          # evicts s1 (max 2 convos)
    recs = log.tail(10)
    sessions = {r["session_id"] for r in recs}
    assert sessions == {"s2", "s3"}, sessions
    # raw tool result is NOT stored — only a digest
    r0 = [r for r in recs if r["session_id"] == "s2"][0]
    log.record(session_id="s2", user_msg="pnl", reply="z", mode="explanation",
               validator="pass", tool_calls=[{"name": "get_pnl", "args": {},
                                             "result": {"secret": 999}}])
    got = log.tail(10)[-1]
    assert got["tool_calls"][0]["result_digest"].startswith("sha256:")
    assert "999" not in json.dumps(got)
    print("audit smoke test: OK (100-convo ring buffer; results hashed, not stored)")
