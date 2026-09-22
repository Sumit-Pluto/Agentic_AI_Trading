"""Macro / News regime agent family.

Reads the news module's persisted state (state/news_state.json, written by
news/service.py every ~60 s) — file-based on purpose: the news hub may live
in another process. If the file is missing or stale (>20 min) the leaf
returns (None, ...) and the tree skips it — never guesses.

Scoring (leaf ``macro_news``, 0-100, 50 = neutral):
  BUY  = 50 + 25×risk_score − event_penalty + fii_bonus
  SELL = 50 − 25×risk_score − event_penalty − fii_bonus

  * risk_score ∈ [−1, +1] from news/weighted.py (+1 = risk-on). Risk-on
    supports buying; risk-off SUPPORTS selling, hence the sign flip.
  * event_penalty: 15 if any macro event with impact 3 lands within the
    next 24 h, else 8 if an impact-2 event is dated today, else 0.
    Event uncertainty hurts conviction in BOTH directions, so it is
    subtracted from both scores.
  * fii_bonus: ±6 by sign of fii_net_cr (0 when absent). FII buying
    supports BUY; FII selling supports SELL — mirrored by subtraction.

build() returns a BRANCH (key='macro') with MacroNews as its first child so
future siblings (micro flows, DOM, commodity links) drop in beside it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..base import QuantAgent

IST = timezone(timedelta(hours=5, minutes=30))
STALE_SECONDS = 20 * 60.0
MACRO_LLM_STALE = 30 * 60.0            # macro_job refreshes ~15 min; skip if older
MACRO_LLM_PATH = "state/macro_llm.json"


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def _event_penalty(events, now: datetime) -> tuple[float, str]:
    """15: impact-3 event inside the next 24h; 8: impact-2 today; else 0."""
    today = now.date()
    tomorrow = today + timedelta(days=1)
    penalty, why = 0.0, "none <24h"
    for e in events or []:
        if not isinstance(e, dict):
            continue
        try:
            impact = int(e.get("impact") or 0)
        except (TypeError, ValueError):
            continue
        d = str(e.get("date") or "")
        try:
            edate = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        name = str(e.get("event") or "?")
        if impact >= 3 and edate in (today, tomorrow):
            t = str(e.get("time") or "")
            if t:
                try:
                    hh, mm = t.split(":")[:2]
                    edt = datetime(edate.year, edate.month, edate.day,
                                   int(hh), int(mm), tzinfo=now.tzinfo)
                    if not (0 <= (edt - now).total_seconds() <= 86400):
                        continue
                except (TypeError, ValueError):
                    pass                    # unparseable time -> date rule
            if penalty < 15.0:
                penalty, why = 15.0, f"impact-3 {name} <24h"
        elif impact == 2 and edate == today and penalty < 8.0:
            penalty, why = 8.0, f"impact-2 {name} today"
    return penalty, why


class MacroNews(QuantAgent):
    key = "macro_news"
    name = "Macro / News Regime"
    description = ("News-module regime read: tape risk score, macro event "
                   "calendar proximity, FII/DII net flows")
    default_enabled = True
    default_weight_buy = 0.5
    default_weight_sell = 0.5

    def compute(self, ctx):
        try:
            from news import schema
        except Exception as e:                        # noqa: BLE001
            return None, f"news module unavailable: {e}"
        state = schema.read_state()
        if not isinstance(state, dict):
            return None, "news module not running"
        gen = _parse_iso(state.get("generated_at"))
        now = datetime.now(timezone.utc)
        if gen is None or (now - gen).total_seconds() > STALE_SECONDS:
            return None, "news module not running (state stale)"

        try:
            risk = max(-1.0, min(1.0, float(state.get("risk_score") or 0.0)))
        except (TypeError, ValueError):
            risk = 0.0
        risk_term = 25.0 * risk

        events = (state.get("calendars") or {}).get("macro") or []
        penalty, penalty_why = _event_penalty(events, datetime.now(IST))

        fii = state.get("fii_dii")
        net = fii.get("fii_net_cr") if isinstance(fii, dict) else None
        if isinstance(net, (int, float)) and net != 0:
            fii_bonus = 6.0 if net > 0 else -6.0
            fii_txt = f"FII {net:+,.0f}cr→{fii_bonus:+.0f}"
        else:
            fii_bonus, fii_txt = 0.0, "FII n/a→+0"

        sell = str(getattr(ctx, "direction", "BUY")).upper() == "SELL"
        if sell:      # risk-off supports SELL: both regime terms flip sign
            score = 50.0 - risk_term - penalty - fii_bonus
        else:
            score = 50.0 + risk_term - penalty + fii_bonus
        score = max(0.0, min(100.0, score))

        detail = (f"{'SELL' if sell else 'BUY'}: 50 "
                  f"{'−' if sell else '+'} 25×risk({risk:+.2f}) "
                  f"− events({penalty:.0f}: {penalty_why}) "
                  f"{'−' if sell else '+'} {fii_txt} = {score:.1f}")
        return score, detail


class MacroLLM(QuantAgent):
    """The macrostructure read: an LLM (Qwen3.5-9B) synthesizes the news
    snapshot into a directional bias × confidence, cached to
    state/macro_llm.json by assistant.macro_job. This leaf just reads that
    file — the trading path NEVER awaits the LLM, and if the cache is stale or
    the endpoint is down the leaf returns (None, ...) and the tree skips it."""

    key = "macro_llm"
    name = "Macrostructure LLM Read"
    description = ("LLM macro synthesis: directional bias × confidence from the "
                   "news/tape/FII/DII snapshot (cached; degrades when stale)")
    default_enabled = True
    default_weight_buy = 0.4
    default_weight_sell = 0.4

    def compute(self, ctx):
        import json
        import os
        try:
            with open(MACRO_LLM_PATH) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None, "macro LLM cache missing (endpoint/job not running)"
        gen = _parse_iso(d.get("generated_at"))
        now = datetime.now(timezone.utc)
        if gen is None or (now - gen).total_seconds() > MACRO_LLM_STALE:
            return None, "macro LLM cache stale"
        try:
            bias = max(-1.0, min(1.0, float(d.get("bias") or 0.0)))
            conf = max(0.0, min(1.0, float(d.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            return None, "macro LLM cache malformed"
        sell = str(getattr(ctx, "direction", "BUY")).upper() == "SELL"
        signed = -bias if sell else bias      # bullish bias supports BUY
        score = max(0.0, min(100.0, 50.0 + 30.0 * signed * conf))
        regime = str(d.get("regime") or "?")
        return score, (f"{'SELL' if sell else 'BUY'}: 50 + 30×bias({bias:+.2f})"
                       f"×conf({conf:.2f}) [{regime}] = {score:.1f}")


class MacroBranch(QuantAgent):
    key = "macro"
    name = "Macro / Micro / Market"
    description = ("Macro regime family: news risk score, event calendar, "
                   "FII/DII flows, LLM macrostructure read")
    default_weight_buy = 0.5
    default_weight_sell = 0.5


def build() -> QuantAgent:
    """Macro family branch — news regime + LLM macrostructure read."""
    return MacroBranch(children=[MacroNews(), MacroLLM()])


# ── offline self-test (fixture state via schema.write_state, no network) ─
if __name__ == "__main__":
    import os
    import sys
    import tempfile
    from types import SimpleNamespace

    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    from news import schema

    # redirect persistence away from the real state dir
    tmp = tempfile.mkdtemp(prefix="macro_agent_test_")
    schema.STATE_DIR = tmp
    schema.NEWS_STATE_PATH = os.path.join(tmp, "news_state.json")

    class StubCfg:
        def enabled(self, key, default=True):
            return default

        def weight(self, key, symbol, direction, db, ds):
            return db if direction == "BUY" else ds

        def threshold(self, key):
            return 50.0

    def ctx(direction):
        return SimpleNamespace(symbol="RELIANCE", direction=direction)

    now_ist = datetime.now(IST)
    today = now_ist.date().isoformat()

    # 1) no state file at all -> skipped
    leaf = MacroNews()
    s, d = leaf.compute(ctx("BUY"))
    assert s is None and "not running" in d, (s, d)

    # 2) fresh fixture: risk-on 0.4, impact-3 event today, FII buying
    fixture = schema.empty_state()
    fixture.update({
        "generated_at": now_ist.isoformat(timespec="seconds"),
        "risk_score": 0.4,
        "fii_dii": {"date": today, "fii_net_cr": 1500.0,
                    "dii_net_cr": -300.0},
    })
    fixture["calendars"]["macro"] = [
        {"date": today, "time": None, "country": "IN",
         "event": "RBI MPC decision", "impact": 3, "actual": None,
         "forecast": None, "previous": None, "source": "events.json"}]
    schema.write_state(fixture)

    b, bd = leaf.compute(ctx("BUY"))       # 50 + 10 - 15 + 6 = 51
    s2, sd = leaf.compute(ctx("SELL"))     # 50 - 10 - 15 - 6 = 19
    assert abs(b - 51.0) < 1e-9, (b, bd)
    assert abs(s2 - 19.0) < 1e-9, (s2, sd)
    assert abs((b + s2) - (100.0 - 2 * 15.0)) < 1e-9   # mirror identity
    for txt in (bd, sd):
        assert "risk(+0.40)" in txt and "events(15" in txt \
            and "+1,500cr" in txt, txt

    # 3) mirror with risk-off + FII selling: SELL must beat BUY
    fixture["risk_score"] = -0.6
    fixture["fii_dii"]["fii_net_cr"] = -2200.0
    fixture["calendars"]["macro"] = []
    schema.write_state(fixture)
    b2, _ = leaf.compute(ctx("BUY"))       # 50 - 15 - 6 = 29
    s3, _ = leaf.compute(ctx("SELL"))      # 50 + 15 + 6 = 71
    assert abs(b2 - 29.0) < 1e-9 and abs(s3 - 71.0) < 1e-9, (b2, s3)

    # 4) impact-2 today penalty = 8; no FII -> bonus 0
    fixture["risk_score"] = 0.0
    fixture["fii_dii"] = None
    fixture["calendars"]["macro"] = [
        {"date": today, "time": None, "country": "US",
         "event": "CPI", "impact": 2, "actual": None,
         "forecast": None, "previous": None, "source": "rapidapi"}]
    schema.write_state(fixture)
    b3, d3 = leaf.compute(ctx("BUY"))
    assert abs(b3 - 42.0) < 1e-9, (b3, d3)

    # 5) stale state (>20 min) -> skipped
    fixture["generated_at"] = (now_ist - timedelta(minutes=25)) \
        .isoformat(timespec="seconds")
    schema.write_state(fixture)
    s4, d4 = leaf.compute(ctx("BUY"))
    assert s4 is None and "stale" in d4, (s4, d4)

    # 6) branch evaluate via build() with the stub cfg
    fixture["generated_at"] = now_ist.isoformat(timespec="seconds")
    schema.write_state(fixture)
    tree = build().evaluate(ctx("BUY"), StubCfg())
    assert tree.key == "macro" and tree.available and tree.score == 42.0
    kids = {k.key: k for k in tree.children}
    assert "macro_news" in kids and kids["macro_news"].score == 42.0

    print("macro.py self-test OK: BUY", b, "SELL", s2,
          "| impact-2 BUY", b3, "| branch", tree.score)
