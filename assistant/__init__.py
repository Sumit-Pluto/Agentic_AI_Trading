"""assistant/ — LLM trading assistant (Qwen/Qwen3.5-9B via RunPod, OpenAI API).

Tools over the live system (positions/P&L/score/quotes/chain/VIX/candles/news-
macro/swing/OI/watchlist/docs) + a tool-calling loop + guardrails (numbers from
tools only; explain-not-advise) + audit. Build/test against a MockLLM; go live
by filling LLM_BASE_URL / LLM_API_KEY in .env (model defaults to
``Qwen/Qwen3.5-9B``). See docs/LLM_ASSISTANT_DEPLOYMENT.md.
"""
from __future__ import annotations

from assistant.agent import Assistant
from assistant.audit import AuditLog
from assistant.client import LLMClient, MockLLM, default_client
from assistant.rag import DocIndex
from assistant.tools import ToolContext


def build_assistant(hub=None, scanner=None, client=None, audit=None):
    """Wire a ready-to-use Assistant. Uses the configured RunPod client by
    default (unavailable until .env is filled); pass a MockLLM to build/test."""
    try:
        rag = DocIndex()
    except Exception:
        rag = None
    ctx = ToolContext(hub=hub, scanner=scanner, rag=rag)
    return Assistant(client or default_client(), ctx,
                     audit=audit if audit is not None else AuditLog())


__all__ = ["Assistant", "AuditLog", "LLMClient", "MockLLM", "DocIndex",
           "ToolContext", "build_assistant"]
