"""assistant/guardrails.py — the product guardrails, model-agnostic.

1. NUMBERS FROM TOOLS ONLY. The system prompt forbids stating any price / P&L /
   OI / score / news figure not obtained from a tool; :func:`validate_numbers`
   is the *actual* guarantee — it checks every number in the reply against the
   turn's tool results and flags fabrications (the agent retries once, then
   refuses).
2. EXPLANATION, NOT ADVICE. :func:`classify_intent` flags advice-seeking
   ("should I buy X"); the prompt makes the model explain the data + scores and
   decline to advise. A SEBI disclaimer is stamped on every reply.

These are enforced in Python around the model — no model gives them for free.
"""
from __future__ import annotations

import json
import re

DISCLAIMER = ("Informational only — this explains what the system computed and "
              "is not investment advice or a recommendation to buy/sell. Verify "
              "figures against your broker terminal.")

SYSTEM_PROMPT = """You are the assistant for a Shoonya-based algo-trading system \
(Indian F&O, Cash and MCX). You help the user understand their trades, the \
system's signals and macro context, and you educate them on market terminology.

You are running on the Qwen3.5-9B model with tool access. Follow these rules \
without exception:

1. EVERY number you state — prices, P&L, OI, VIX, scores, % changes, news \
figures — MUST come from a tool call in THIS conversation. Never state a number \
from memory, training data or estimation. If you don't have a number, call the \
right tool; if a tool has no data, say so plainly. Do not guess.

2. Prefer tools for anything factual about the account or the live market: \
positions, P&L, quotes, option chain, VIX, candles, news/macro, swing signals, \
OI-strategy hits, watchlist P&L, and the agent score for a symbol.

3. EXPLAIN, DON'T ADVISE. You may explain what happened, why the system scored \
something, and what a term means. You must NOT tell the user whether to buy, \
sell, hold, or predict prices. If asked for a recommendation, explain the \
relevant data and scores, then decline to advise and note it's their decision.

4. Use search_docs to explain how the system works or define terminology, \
grounding your explanation in the project's own documentation.

Be concise and precise. Cite the tool you used when stating figures."""

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_ADVICE_RE = re.compile(
    r"\b(should i|shall i|do i|is it a good|good (buy|sell)|"
    r"worth (buying|selling)|will it (go|rise|fall|drop|move)|"
    r"recommend|advice|advise|what should i (do|buy|sell)|"
    r"is (this|it) a buy|is (this|it) a sell)\b", re.I)


def _to_float(tok: str):
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.findall(text or ""):
        v = _to_float(m)
        if v is not None:
            out.append(v)
    return out


def _collect_tool_numbers(tool_results) -> list[float]:
    """Every numeric value that appeared in the turn's tool outputs."""
    nums: list[float] = []

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            nums.append(float(o))
        elif isinstance(o, str):
            for v in extract_numbers(o):
                nums.append(v)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    for r in tool_results:
        walk(r)
    return nums


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(b))


# Index NAMES whose suffix number must not be mistaken for a figure
# (S&P 500, Nifty 50, Nasdaq 100, Dow 30, Russell 2000, Nikkei 225 …). A real
# level like "Nifty 24180" keeps its number and still requires grounding.
_INDEX_NAME_RE = re.compile(
    r"\b(s\s*&\s*p|s&p|nasdaq|dow|russell|ftse|nikkei|nifty|sensex|bse|cnx|"
    r"hang\s*seng)\s*(?:jones\s+)?(?:50|100|200|400|500|1000|2000|30|225)\b", re.I)


def _strip_index_names(text: str) -> str:
    return _INDEX_NAME_RE.sub(lambda m: m.group(1), text or "")


def validate_numbers(reply: str, tool_results: list, tol: float = 0.01,
                     whitelist_max: float = 50.0) -> tuple[bool, list[float]]:
    """Return (ok, unsupported_numbers). A number in the reply is allowed if it
    matches (by value OR magnitude, within ``tol``) any number from the tool
    results, OR it is small (< 10 = ratio/%/RR) or a small integer (≤
    ``whitelist_max`` = date/count/ordinal). Index names (S&P 500 …) are stripped
    first so their suffix isn't read as a figure."""
    supported = _collect_tool_numbers(tool_results)
    bad = []
    for n in extract_numbers(_strip_index_names(reply)):
        if abs(n) < 10.0 or (float(n).is_integer() and abs(n) <= whitelist_max):
            continue
        # match by value OR magnitude — the model writes a stored -300 flow as
        # "net sell ₹300", so |reply| vs |tool| must also count as grounded.
        if any(_close(n, s, tol) or _close(abs(n), abs(s), tol) for s in supported):
            continue
        bad.append(n)
    return (not bad, bad)


def classify_intent(user_msg: str) -> str:
    """'advice' if the user is asking for a buy/sell recommendation or a price
    prediction, else 'explanation'."""
    return "advice" if _ADVICE_RE.search(user_msg or "") else "explanation"


def strip_reasoning(text: str) -> str:
    """Qwen3.5's thinking mode leaks a <think>…</think> chain-of-thought into the
    reply content. Keep only the final answer (after the last </think>) so the
    user sees the clean answer and the number-validator isn't tripped by numbers
    the model was merely reasoning about."""
    if not text:
        return text
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    return text.strip()


CORRECTION = ("Some numbers in your draft were not present in the tool results. "
              "Rewrite using ONLY figures returned by the tools this turn; drop "
              "or re-fetch anything else.")

REFUSAL = ("I can only state figures that come from the system's tools, and I "
           "couldn't ground some numbers just now. Ask me for the specific "
           "quote, P&L or score and I'll fetch it.")

ADVICE_STEER = (
    "The user is asking for a buy/sell recommendation or a price prediction. Do "
    "NOT tell them whether to buy/sell/hold and do NOT predict prices. Instead, "
    "CALL the relevant tools (get_score, get_positions, get_news_macro, "
    "get_oi_signals, get_swing_signals) and present the data objectively, then "
    "state plainly that the decision is theirs and you don't give investment "
    "advice.")
