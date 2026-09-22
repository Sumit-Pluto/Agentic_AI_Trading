"""End-to-end assistant test with a MockLLM (no endpoint, no network).

Verifies: the tool-calling loop executes a tool and answers using its numbers
(validator PASS); the number-validator catches a fabricated figure and, after a
correction the model ignores, the turn is REFUSED; advice intent is flagged; and
the turn is audit-logged with tool provenance.

Run: python -m assistant.test_assistant
"""
import os
import sys
import tempfile
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant.agent import Assistant
from assistant.audit import AuditLog
from assistant.client import MockLLM
from assistant.guardrails import REFUSAL, classify_intent
from assistant.tools import ToolContext


class MockHub:
    def price(self, sym, seg=None):
        return {"RELIANCE": 2929.0}.get(sym)

    def india_vix(self):
        return 13.5


class MockScanner:
    def __init__(self):
        self.hub = MockHub()
        self.exits = NS(positions={})
        self.paper = NS(tail=lambda n: [])
        self.watchlist = None

    def evaluate(self, symbol, direction, segment=None):
        return {"symbol": symbol, "direction": direction, "segment": "FNO",
                "price": 2929.0, "score": 72, "threshold": 60, "accepted": True,
                "vetoed_by": None, "tree": {"children": [{"key": "smc", "score": 70}]}}


def _tc(name, args):
    return {"id": "call_1", "name": name, "arguments": args}


def _assistant(script, audit):
    ctx = ToolContext(hub=MockHub(), scanner=MockScanner(), rag=None)
    return Assistant(MockLLM(script), ctx, audit=audit)


def main():
    audit = AuditLog(os.path.join(tempfile.mkdtemp(), "audit.jsonl"))

    # 1) tool call -> grounded answer (validator PASS)
    a = _assistant([
        {"tool_calls": [_tc("get_score", {"symbol": "RELIANCE", "direction": "BUY"})]},
        {"content": "RELIANCE BUY scores 72 vs threshold 60 — accepted. Spot 2929.0."},
    ], audit)
    r = a.chat("s1", "why would RELIANCE fire a buy?")
    assert r["validator"] == "pass", r
    assert "get_score" in r["tools_used"]
    assert "2929.0" in r["reply"]
    assert r["disclaimer"]

    # 2) fabricated number -> validator catches -> correction ignored -> REFUSED
    a = _assistant([
        {"tool_calls": [_tc("get_score", {"symbol": "RELIANCE", "direction": "BUY"})]},
        {"content": "RELIANCE target is 3100.50 and support 2810.25."},   # not in tools
        {"content": "RELIANCE target is 3100.50."},                        # still fabricated
    ], audit)
    r = a.chat("s2", "what's the target?")
    assert r["validator"] == "refused" and r["reply"] == REFUSAL, r

    # 3) advice intent is flagged (explain-not-advise)
    a = _assistant([{"content": "Here's what the data shows…"}], audit)
    r = a.chat("s3", "should I buy RELIANCE right now?")
    assert r["mode"] == "advice", r
    assert classify_intent("what is my current pnl") == "explanation"

    # 4) audit captured provenance (tool name + hashed result, not raw)
    recs = audit.tail(10)
    s1 = [x for x in recs if x["session_id"] == "s1"][0]
    assert s1["tool_calls"][0]["name"] == "get_score"
    assert s1["tool_calls"][0]["result_digest"].startswith("sha256:")
    assert s1["validator"] == "pass"

    print("assistant e2e test: OK — tool loop grounds answers, validator refuses "
          "fabrications, advice flagged, audit provenance logged")


if __name__ == "__main__":
    main()
