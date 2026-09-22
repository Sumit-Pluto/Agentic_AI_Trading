"""assistant/agent.py — the tool-calling loop with guardrails + audit.

One turn: send the system prompt + running history + the user message with the
tool schemas; execute any tool calls the model makes and feed results back;
when the model produces text, VALIDATE its numbers against this turn's tool
results (guardrail #1). On a validation failure inject one correction and let
the model try again; if it still fabricates, return a safe refusal. Every turn
is audit-logged with tool provenance.
"""
from __future__ import annotations

import json

from assistant import tools as T
from assistant.guardrails import (ADVICE_STEER, CORRECTION, DISCLAIMER, REFUSAL,
                                  SYSTEM_PROMPT, classify_intent,
                                  strip_reasoning, validate_numbers)

MAX_ITERS = 6
# tools that return live market figures — the ones the number-validator must
# guard. An answer grounded ONLY in search_docs (methodology) is exempt: it
# cites doc-defined constants the validator can't match verbatim.
_MARKET_TOOLS = {"get_positions", "get_recent_trades", "get_score", "get_quote",
                 "get_vix", "get_option_chain", "get_candles", "get_news_macro",
                 "get_swing_signals", "get_oi_signals", "get_watchlist"}


class Assistant:
    def __init__(self, client, ctx: "T.ToolContext", audit=None,
                 history_turns: int = 8, temperature: float = 0.4):
        self.client = client
        self.ctx = ctx
        self.audit = audit
        self.history_turns = history_turns
        self.temperature = temperature
        self._sessions: dict[str, list] = {}

    def _history(self, session_id: str) -> list:
        return self._sessions.setdefault(session_id, [])

    @staticmethod
    def _assistant_toolcall_msg(resp: dict, tcs: list) -> dict:
        return {"role": "assistant", "content": resp.get("content") or None,
                "tool_calls": [{"id": tc["id"], "type": "function",
                                "function": {"name": tc["name"],
                                             "arguments": json.dumps(tc.get("arguments") or {})}}
                               for tc in tcs]}

    def _run(self, messages, schemas, turn_results, audit_calls) -> tuple[str, str]:
        corrected = False
        for _ in range(MAX_ITERS):
            resp = self.client.chat(messages, tools=schemas,
                                    temperature=self.temperature)
            tcs = resp.get("tool_calls") or []
            if tcs:
                messages.append(self._assistant_toolcall_msg(resp, tcs))
                for tc in tcs:
                    result = T.execute(tc["name"], tc.get("arguments") or {}, self.ctx)
                    turn_results.append(result)
                    audit_calls.append({"name": tc["name"],
                                        "args": tc.get("arguments") or {},
                                        "result": result})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(result, default=str)[:4000]})
                continue
            reply = strip_reasoning(resp.get("content") or "")
            ok, _bad = validate_numbers(reply, turn_results)
            if ok:
                return reply or REFUSAL, "pass"
            # Methodology answers grounded ONLY in the docs may cite doc-defined
            # constants (thresholds, multipliers) the validator can't match
            # verbatim — the doc IS the source, so don't hard-refuse those.
            used = {c["name"] for c in audit_calls}
            if used and used.isdisjoint(_MARKET_TOOLS):
                return reply, "docs-lenient"
            if corrected:
                return REFUSAL, "refused"
            corrected = True
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": CORRECTION})
        return REFUSAL, "refused"

    def chat(self, session_id: str, user_msg: str) -> dict:
        mode = classify_intent(user_msg)
        history = self._history(session_id)
        # the advice steer folds into the single LEADING system message —
        # servers require system messages to come first.
        sys_content = SYSTEM_PROMPT
        if mode == "advice":
            sys_content += "\n\n" + ADVICE_STEER
        messages = [{"role": "system", "content": sys_content}]
        messages += history[-2 * self.history_turns:]
        messages.append({"role": "user", "content": user_msg})

        turn_results: list = []
        audit_calls: list = []
        reply, validator = self._run(messages, T.tool_schemas(),
                                     turn_results, audit_calls)

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": reply})
        if len(history) > 2 * self.history_turns:
            del history[:len(history) - 2 * self.history_turns]

        if self.audit is not None:
            try:
                self.audit.record(session_id=session_id, user_msg=user_msg,
                                  tool_calls=audit_calls, reply=reply,
                                  mode=mode, validator=validator)
            except Exception:
                pass

        return {"reply": reply, "mode": mode, "disclaimer": DISCLAIMER,
                "tools_used": [c["name"] for c in audit_calls],
                "validator": validator}
