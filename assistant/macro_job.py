"""assistant/macro_job.py — periodic LLM macro synthesis for the macro agent.

Reads the news hub snapshot (risk score, market tape, FII/DII, calendars) and
asks the LLM (Qwen3.5-9B) for a STRUCTURED macro read — {bias, confidence,
regime, drivers, summary} — written atomically to state/macro_llm.json. The
`quant.agents.macro.MacroLLM` leaf consumes that file, so the trading path never
awaits the LLM: it reads the cache and degrades gracefully when stale.

Runs on its own slow cadence (MACRO_LLM_SECONDS, default 900s) on a background
thread. No-op when the LLM endpoint isn't configured.

Offline smoke test (mock client): ``python -m assistant.macro_job``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime

log = logging.getLogger("macro_llm")

STORE = os.path.join("state", "macro_llm.json")

MACRO_PROMPT = """You are a macro analyst for Indian F&O intraday trading. From \
the market snapshot below, return ONLY a JSON object (no prose, no code fence) \
with these keys:
  "bias": number in [-1,1]  (negative = bearish, positive = bullish for NIFTY intraday)
  "confidence": number in [0,1]
  "regime": one of "risk-on", "risk-off", "event-wait"
  "drivers": array of up to 4 short strings
  "summary": one sentence
Base it ONLY on the data provided; do not invent numbers.

SNAPSHOT:
"""


class MacroJob:
    def __init__(self, client, state_dir: str = "state",
                 interval_sec: int | None = None, temperature: float = 0.2):
        self.client = client
        self.state_dir = state_dir
        self.interval_sec = interval_sec or int(os.getenv("MACRO_LLM_SECONDS", "900"))
        self.temperature = temperature
        self._stop = threading.Event()

    def _snapshot(self) -> dict | None:
        try:
            with open(os.path.join(self.state_dir, "news_state.json")) as f:
                s = json.load(f)
        except (OSError, ValueError):
            return None
        return {"risk_score": s.get("risk_score"),
                "market_tape": s.get("market_tape"),
                "fii_dii": s.get("fii_dii"),
                "top_news": [w.get("title") for w in (s.get("weighted") or [])[:6]],
                "calendars": s.get("calendars") or {}}

    @staticmethod
    def _parse(text: str) -> dict | None:
        text = text or ""
        # A reasoning model may emit a <think> preamble and/or prose around the
        # JSON. Strip thinking, then scan every brace-object and take the first
        # that BOTH parses and carries the macro fields — a greedy first-to-last
        # `\{.*\}` match breaks on stray braces or a truncated tail.
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1]
        for cand in (re.findall(r"\{[^{}]*\}", text, re.S) or []):
            try:
                d = json.loads(cand)
            except ValueError:
                continue
            if not (isinstance(d, dict) and "bias" in d and "confidence" in d):
                continue
            try:
                return {"bias": max(-1.0, min(1.0, float(d.get("bias", 0)))),
                        "confidence": max(0.0, min(1.0, float(d.get("confidence", 0)))),
                        "regime": str(d.get("regime", "")),
                        "drivers": [str(x) for x in (d.get("drivers") or [])][:4],
                        "summary": str(d.get("summary", ""))}
            except (TypeError, ValueError):
                continue
        return None

    def run_once(self) -> dict | None:
        if not self.client.available():
            return None
        snap = self._snapshot()
        if not snap:
            return None
        messages = [{"role": "user",
                     "content": MACRO_PROMPT + json.dumps(snap, default=str)}]
        try:
            resp = self.client.chat(messages, tools=None,
                                    temperature=self.temperature, max_tokens=1500)
        except Exception as e:
            log.debug("macro LLM call failed: %s", e)
            return None
        out = self._parse(resp.get("content") or "")
        if out is None:
            return None
        out["generated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save(out)
        return out

    def _save(self, out: dict) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            path = os.path.join(self.state_dir, "macro_llm.json")
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(out, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def start(self) -> None:
        threading.Thread(target=self._run, name="macro-llm", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                log.warning("macro job error: %s", e)
            if self._stop.wait(self.interval_sec):
                break


if __name__ == "__main__":
    import tempfile
    from assistant.client import MockLLM

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "news_state.json"), "w") as f:
        json.dump({"risk_score": 0.4, "market_tape": {"spx": {"chg_pct": 0.8}},
                   "fii_dii": {"fii_net_cr": 1500}, "weighted": []}, f)
    client = MockLLM([{"content": 'Here you go: {"bias": 0.35, "confidence": 0.6, '
                       '"regime": "risk-on", "drivers": ["FII buying", "SPX up"], '
                       '"summary": "Mildly bullish, risk-on."} thanks'}])
    job = MacroJob(client, state_dir=d, interval_sec=999)
    out = job.run_once()
    assert out and abs(out["bias"] - 0.35) < 1e-9 and out["regime"] == "risk-on", out
    assert os.path.exists(os.path.join(d, "macro_llm.json"))
    # malformed / no-JSON reply -> None (never writes garbage)
    job2 = MacroJob(MockLLM([{"content": "no json here"}]), state_dir=d)
    assert job2.run_once() is None
    print("macro_job smoke test: OK — parsed structured bias, atomic-wrote cache")
