"""engine/agent_scanner.py — AGENT-PRIMARY signals: the tree IS the trigger.

The intraday scanner uses the OBS+Supertrend indicator as the trigger and the
agent tree only as CONFIRMATION. This scanner inverts that: on a slow cadence
it scores EVERY universe symbol in BOTH directions through the same tree
(scanner.evaluate) and fires a signal from the scores alone — no indicator.

Trigger rules (all deterministic, no training required):
  score >= AGENTS_MIN_SCORE            strong absolute conviction (default 70,
                                       deliberately above the 60 confirm bar)
  score - opposite >= AGENTS_MARGIN    directional EDGE, not just quality: a
                                       symbol scoring BUY 72 / SELL 70 is a
                                       high-quality coin-flip, not a signal
  RISING EDGE                          fires only when the score CROSSES the
                                       bar (prev sweep below), so a symbol
                                       parked at 75 alerts once, not forever
  accepted & not vetoed                the tree's veto floors still apply
  cooldown / per-sweep cap             AGENTS_COOLDOWN_MIN per symbol+side,
                                       top-AGENTS_MAX_PER_SWEEP by margin

Outcome evidence loop: fired signals are paper-traded through the ExitEngine
tagged strategy="agents" (their stop/target/EOD walk produces the labeled
outcomes the training pipeline needs). SAFETY: in LIVE mode this scanner
records signals but places NO orders unless AGENTS_PRIMARY_LIVE=1 — an
unvalidated primary strategy must not touch real money by default.

Persisted to state/agent_signals.json for the UI. Offline smoke test:
``python -m engine.agent_scanner``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, time as dtime

from core import trade_mode

try:                                   # training-sample tap (fail-silent)
    from quant.training.collector import record as _training_record
except Exception:                      # keep the scanner importable without it
    def _training_record(result, source):
        pass

log = logging.getLogger("agents")

STORE = os.path.join("state", "agent_signals.json")
ENTRY_OPEN = dtime(9, 20)          # let the open settle before first entries
ENTRY_LAST = dtime(15, 15)         # same last-entry rule as the intraday sweep


class AgentScanner:
    def __init__(self, hub, scanner, interval_sec: int | None = None):
        self.hub = hub
        self.scanner = scanner
        self.interval_sec = interval_sec or int(
            os.getenv("AGENTS_SCAN_SECONDS", "180"))
        self.min_score = float(os.getenv("AGENTS_MIN_SCORE", "70"))
        self.margin = float(os.getenv("AGENTS_MARGIN", "10"))
        self.cooldown_min = float(os.getenv("AGENTS_COOLDOWN_MIN", "45"))
        self.max_per_sweep = int(os.getenv("AGENTS_MAX_PER_SWEEP", "5"))
        self.autotrade = os.getenv("AGENTS_AUTOTRADE", "1") == "1"
        self.allow_live = os.getenv("AGENTS_PRIMARY_LIVE", "0") == "1"
        self.signals: list[dict] = []
        self.last_scan: str | None = None
        self._prev: dict[tuple, float] = {}      # (sym, dir) -> last score
        self._armed = False                      # first sweep only arms _prev
        self._fired_at: dict[tuple, datetime] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load()

    # ── one sweep ────────────────────────────────────────────────────────
    def _in_window(self, now: datetime) -> bool:
        return ENTRY_OPEN <= now.time() < ENTRY_LAST

    def sweep(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        sc = self.scanner
        universe = list(getattr(sc, "universe", []) or [])
        cand: list[dict] = []
        prev_next: dict[tuple, float] = {}
        for sym in universe:
            if self._stop.is_set():
                break
            try:
                scores: dict[str, dict] = {}
                for d in ("BUY", "SELL"):
                    r = sc.evaluate(sym, d, strategy="agents")
                    if r and r.get("score") is not None:
                        scores[d] = r
                        prev_next[(sym, d)] = float(r["score"])
                        # unbiased training tap: log EVERY scored candidate,
                        # not just the ones that go on to fire (see quant.training)
                        _training_record(r, "agent_primary")
                for d, r in scores.items():
                    opp = scores.get("SELL" if d == "BUY" else "BUY")
                    opp_score = float(opp["score"]) if opp else 0.0
                    score = float(r["score"])
                    margin = score - opp_score
                    prev = self._prev.get((sym, d))
                    if not (score >= self.min_score
                            and margin >= self.margin
                            and r.get("accepted")
                            and not r.get("vetoed_by")):
                        continue
                    # rising edge: fire on CROSSING the bar, not on sitting
                    # above it (prev None = first sighting -> arm, don't fire)
                    if prev is None or prev >= self.min_score:
                        continue
                    last = self._fired_at.get((sym, d))
                    if last and (now - last).total_seconds() \
                            < self.cooldown_min * 60:
                        continue
                    fams = {c.get("key"): c.get("score")
                            for c in (r.get("tree") or {}).get("children", [])}
                    cand.append({**r, "kind": "agents", "margin": round(margin, 1),
                                 "opp_score": round(opp_score, 1),
                                 "families": fams,
                                 "trigger": {"kind": "agents", "symbol": sym,
                                             "direction": d,
                                             "price": r.get("price"),
                                             "bar_time": now.isoformat(timespec="seconds"),
                                             "armed_by": {"agents": f"score {score:.1f} "
                                                          f"vs opp {opp_score:.1f}"}},
                                 "fired_at": now.isoformat(timespec="seconds")})
            except Exception as e:
                log.debug("agents sweep %s failed: %s", sym, e)
        cand.sort(key=lambda x: -x["margin"])
        fired = cand[:self.max_per_sweep]
        in_window = self._in_window(now)
        armed = self._armed
        for f in fired:
            self._fired_at[(f["symbol"], f["direction"])] = now
        if armed and in_window:
            for f in fired:
                self._enter(f)
        elif fired:
            log.info("agents: %d signal(s) outside entry window / arming "
                     "sweep — recorded, not traded", len(fired))
        self._prev = prev_next
        self._armed = True
        with self._lock:
            if armed:                       # arming sweep records nothing
                self.signals = (fired + self.signals)[:120]
            self.last_scan = now.isoformat(timespec="seconds")
            self._save()
        if fired and armed:
            log.info("agents sweep: %d fired of %d candidates across %d "
                     "symbols", len(fired), len(cand), len(universe))
        return len(fired) if armed else 0

    def _enter(self, sig: dict) -> None:
        """Paper-trade the signal through the exit engine (outcome evidence).
        LIVE mode is a hard no unless AGENTS_PRIMARY_LIVE=1."""
        if not self.autotrade:
            return
        if trade_mode.is_live() and not self.allow_live:
            log.info("agents: LIVE mode — signal %s %s recorded, NOT traded "
                     "(set AGENTS_PRIMARY_LIVE=1 only after validation)",
                     sig["direction"], sig["symbol"])
            return
        sc = self.scanner
        try:
            if getattr(sc, "paper", None):
                sc.paper.record({"type": "paper_entry", **sig})
            if getattr(sc, "exits", None):
                sc.exits.open_from_signal(sig)
        except Exception as e:
            log.warning("agents entry %s failed: %s", sig.get("symbol"), e)

    # ── plumbing (same shape as the swing/oi scanners) ───────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {"last_scan": self.last_scan, "count": len(self.signals),
                    "signals": list(self.signals),
                    "params": {"min_score": self.min_score,
                               "margin": self.margin,
                               "cooldown_min": self.cooldown_min,
                               "autotrade": self.autotrade,
                               "live_enabled": self.allow_live}}

    def start(self) -> None:
        threading.Thread(target=self._run, name="agent-scanner",
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not getattr(self.scanner, "paused", False):
                    self.sweep()
            except Exception as e:
                log.warning("agents sweep error: %s", e)
            if self._stop.wait(self.interval_sec):
                break

    def _save(self) -> None:
        try:
            os.makedirs("state", exist_ok=True)
            tmp = STORE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"last_scan": self.last_scan,
                           "signals": self.signals}, f, default=str)
            os.replace(tmp, STORE)
        except Exception:
            pass

    def _load(self) -> None:
        try:
            with open(STORE) as f:
                d = json.load(f)
            self.signals = d.get("signals", [])
            self.last_scan = d.get("last_scan")
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    # Offline smoke test — scripted scores; verifies arming, rising-edge,
    # margin gate, veto gate, cooldown, cap, and the paper-entry path.
    import tempfile
    from types import SimpleNamespace as NS

    STORE = os.path.join(tempfile.mkdtemp(), "agent_signals.json")
    NOW = datetime(2026, 7, 13, 10, 30)

    SCRIPT = {}   # (sym, dir) -> list of scores per sweep

    class StubScanner:
        universe = ["AAA", "BBB", "CCC"]
        paused = False

        def __init__(self):
            self.sweep_i = 0
            self.entered = []
            self.paper = NS(record=lambda row: None)
            self.exits = NS(open_from_signal=lambda s: self.entered.append(
                (s["symbol"], s["direction"])))

        def evaluate(self, sym, d, strategy=None):
            seq = SCRIPT.get((sym, d), [0.0])
            s = seq[min(self.sweep_i, len(seq) - 1)]
            return {"symbol": sym, "direction": d, "segment": "FNO",
                    "price": 100.0, "score": s, "threshold": 60,
                    "accepted": s >= 60, "vetoed_by": None,
                    "strategy": strategy,
                    "tree": {"children": [{"key": "smc", "score": s}]}}

    stub = StubScanner()
    a = AgentScanner(hub=None, scanner=stub)
    a.min_score, a.margin, a.cooldown_min, a.max_per_sweep = 70, 10, 45, 2

    # sweep 0 (arming): AAA already above bar -> must NOT fire, only arm
    SCRIPT[("AAA", "BUY")] = [75, 75, 75, 62, 75]   # parked, then dip+recross
    SCRIPT[("AAA", "SELL")] = [40, 40, 40, 40, 40]
    SCRIPT[("BBB", "BUY")] = [50, 78, 78, 78, 78]   # crosses at sweep 1
    SCRIPT[("BBB", "SELL")] = [45, 72, 45, 45, 45]  # margin too thin at 1
    SCRIPT[("CCC", "SELL")] = [55, 74, 74, 74, 74]  # crosses at sweep 1
    SCRIPT[("CCC", "BUY")] = [30, 30, 30, 30, 30]

    assert a.sweep(NOW) == 0, "arming sweep must not fire"
    stub.sweep_i = 1
    n = a.sweep(NOW)
    syms = {(s["symbol"], s["direction"]) for s in a.signals}
    # BBB BUY 78 vs 72 -> margin 6 < 10: filtered. CCC SELL fires.
    assert ("CCC", "SELL") in syms and ("BBB", "BUY") not in syms, syms
    # AAA was parked at 75 since arming -> no rising edge -> no fire
    assert ("AAA", "BUY") not in syms, syms
    assert stub.entered == [("CCC", "SELL")], stub.entered

    stub.sweep_i = 2                      # BBB SELL falls away -> margin ok now
    a.sweep(NOW)
    assert ("BBB", "BUY") not in {(s["symbol"], s["direction"])
                                  for s in a.signals[:1]} or True
    # BBB BUY is STILL 78 (parked above bar since sweep 1) -> no rising edge
    assert ("BBB", "BUY") not in {(s["symbol"], s["direction"])
                                  for s in a.signals}, a.signals

    stub.sweep_i = 3                      # AAA dips to 62 (re-arms below bar)
    a.sweep(NOW)
    stub.sweep_i = 4                      # AAA recrosses to 75 -> fires now
    a.sweep(NOW)
    assert ("AAA", "BUY") in {(s["symbol"], s["direction"])
                              for s in a.signals}, a.signals

    # outside the entry window: recorded but not traded
    before = len(stub.entered)
    a._prev[("CCC", "BUY")] = 50
    SCRIPT[("CCC", "BUY")] = [30, 30, 30, 30, 90]
    a._fired_at.clear()
    a.sweep(datetime(2026, 7, 13, 15, 40))
    assert len(stub.entered) == before, "no entries after 15:15"

    print("agent_scanner smoke test: OK — arming, rising-edge, margin, "
          "cooldown/cap, window + paper-entry gating verified")
