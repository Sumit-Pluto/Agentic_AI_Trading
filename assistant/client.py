"""assistant/client.py — LLM client (OpenAI-compatible) + a mock for offline build.

The real client targets the RunPod Qwen3.5-9B endpoint via the OpenAI protocol
(vLLM speaks it). It is imported lazily so this module loads even when the
``openai`` package isn't installed and the endpoint isn't up yet.

Both clients expose the SAME normalized interface so the agent loop is
endpoint-agnostic::

    client.chat(messages, tools=None, temperature=..., max_tokens=...) -> {
        "content": str | None,
        "tool_calls": [{"id": str, "name": str, "arguments": dict}],
    }
"""
from __future__ import annotations

import json
import os
from typing import Optional


class LLMClient:
    """OpenAI-compatible chat client (RunPod vLLM / Qwen3.5-9B)."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 120.0):
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self.api_key = api_key or os.getenv("LLM_API_KEY")
        self.model = model or os.getenv("LLM_MODEL", "Qwen/Qwen3.5-9B")
        self.timeout = timeout
        self._client = None

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI            # lazy — only when actually used
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key,
                                  timeout=self.timeout)
        return self._client

    def chat(self, messages: list[dict], tools: Optional[list] = None,
             temperature: float = 0.4, max_tokens: int = 1024) -> dict:
        cli = self._ensure()
        resp = cli.chat.completions.create(
            model=self.model, messages=messages, tools=tools or None,
            temperature=temperature, max_tokens=max_tokens)
        msg = resp.choices[0].message
        tool_calls = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name,
                               "arguments": args})
        return {"content": msg.content, "tool_calls": tool_calls}


class MockLLM:
    """Scripted client for offline build/test — returns queued responses in
    order. Each response is a dict {"content": str|None, "tool_calls": [...]}.
    Records every call it received for assertions."""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict], tools=None, **kw) -> dict:
        self.calls.append({"messages": list(messages), "tools": tools})
        if self.script:
            r = self.script.pop(0)
            return {"content": r.get("content"),
                    "tool_calls": r.get("tool_calls", [])}
        return {"content": "", "tool_calls": []}


def default_client():
    """The configured real client (may be unavailable until .env is filled)."""
    return LLMClient()
