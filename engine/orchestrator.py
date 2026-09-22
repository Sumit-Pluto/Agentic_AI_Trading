"""Orchestrator — hand-rolled 30-second scheduler loop (IST).

No APScheduler, no LangGraph, no LLM, no Telegram: a daemon thread ticks
every 30s, derives the session phase from the IST wall clock and drives:

    pre_open  >=08:50 <09:15 Mon-Fri   once/day warmup: refresh hub scrip
                                       caches (fo_universe) + warm 5m
                                       candles for the first 10 symbols
    open      09:15-15:30    Mon-Fri   scanner runs; pause mirrors ONLY the
                                       user's file kill-switch
    closed    everything else          scanner phase-paused WITHOUT touching
                                       the user kill-switch file
    eod       >=15:35        Mon-Fri   once/day summary: exits.summary() +
                                       today's paper_entry/paper_exit counts
                                       -> paper book {'type':'eod_summary'}

Every tick reconciles  scanner.paused = is_paused() or not market-hours,
so the UI pause button still rules during hours and nights/weekends never
flip the user's own switch.  Every phase change, warmup and eod is
published to the activity journal.  Every step is try/except-guarded.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, time as dtime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from core.activity import activity
    from core.killswitch import is_paused
except ModuleNotFoundError:            # run as a script: add repo root
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    try:
        from core.activity import activity
        from core.killswitch import is_paused
    except Exception:                  # last resort: never crash the caller
        class _NoopActivity:
            def add(self, *args, **kwargs):
                pass

            def recent(self, n=100):
                return []

        activity = _NoopActivity()

        def is_paused() -> bool:
            return False

log = logging.getLogger("orchestrator")

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                      # tz database missing: fixed offset
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# ── session clock (IST) ─────────────────────────────────────────────────
PRE_OPEN_START = dtime(8, 50)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
EOD_AT = dtime(15, 35)
TICK_SECONDS = 30
WARM_SYMBOLS = 10                      # candles warmed during pre-open


class Orchestrator:
    """Phase scheduler around a Scanner + DataHub.  All side effects are
    guarded; a bad tick never kills the loop."""

    def __init__(self, scanner, hub, now_fn=None,
                 tick_seconds: int = TICK_SECONDS):
        self.scanner = scanner
        self.hub = hub
        self.tick_seconds = tick_seconds
        self._now = now_fn or (lambda: datetime.now(IST))   # injectable
        self._stop = threading.Event()
        self.phase: str | None = None
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self._warmup_day = None        # date of the last pre-open warmup
        self._eod_day = None           # date of the last eod summary

    # ── phase from the wall clock ────────────────────────────────────────
    @staticmethod
    def _phase_for(now: datetime) -> str:
        t = now.time()
        if now.weekday() < 5:                       # Mon-Fri
            if PRE_OPEN_START <= t < MARKET_OPEN:
                return "pre_open"
            if MARKET_OPEN <= t <= MARKET_CLOSE:
                return "open"
        return "closed"

    # ── one tick ─────────────────────────────────────────────────────────
    def tick(self):
        try:
            now = self._now()
        except Exception as e:                      # clock injection broke
            self.last_error = f"now(): {e}"
            return

        # 1. phase transition
        try:
            phase = self._phase_for(now)
            if phase != self.phase:
                old = self.phase
                self.phase = phase
                log.info("phase %s -> %s", old, phase)
                activity.add("phase", f"phase {old or 'boot'} -> {phase}",
                             phase=phase)
        except Exception as e:
            self.last_error = f"phase: {e}"
            log.warning("phase step failed: %s", e)

        # 2. reconcile the scanner pause EVERY tick: user kill-switch OR
        #    phase block — never write the kill-switch file from here
        try:
            user = bool(is_paused())
            want = user or self.phase != "open"
            if bool(getattr(self.scanner, "paused", False)) != want:
                self.scanner.paused = want
                why = ("kill-switch" if user else
                       "market hours" if not want else "phase")
                log.info("scanner.paused -> %s (%s)", want, why)
                activity.add("pause",
                             f"scanner {'paused' if want else 'resumed'} "
                             f"({why})", paused=want, killswitch=user,
                             phase=self.phase)
            else:
                self.scanner.paused = want          # idempotent reconcile
        except Exception as e:
            self.last_error = f"pause: {e}"
            log.warning("pause reconcile failed: %s", e)

        # 3. once-a-day pre-open warmup
        try:
            if self.phase == "pre_open" and self._warmup_day != now.date():
                self._warmup_day = now.date()
                self._warmup(now)
        except Exception as e:
            self.last_error = f"warmup: {e}"
            log.warning("warmup failed: %s", e)

        # 4. once-a-day eod summary
        try:
            if (now.weekday() < 5 and now.time() >= EOD_AT
                    and self._eod_day != now.date()):
                self._eod_day = now.date()
                self._eod(now)
        except Exception as e:
            self.last_error = f"eod: {e}"
            log.warning("eod failed: %s", e)

        self.last_tick = now.isoformat(timespec="seconds")

    # ── warmup: refresh scrip caches + warm the first N candle series ───
    def _warmup(self, now: datetime):
        symbols: list = []
        try:
            symbols = list(self.hub.fo_universe() or [])
        except Exception as e:
            log.warning("warmup fo_universe failed: %s", e)
            symbols = list(getattr(self.scanner, "universe", []) or [])
        warmed = 0
        for sym in symbols[:WARM_SYMBOLS]:
            try:
                if self.hub.candles_5m(sym) is not None:
                    warmed += 1
            except Exception as e:
                log.debug("warmup candles %s failed: %s", sym, e)
        news_age_min = vix = None
        try:                                        # local file only
            from news.schema import read_state
            gen = (read_state() or {}).get("generated_at")
            if gen:
                g = datetime.fromisoformat(str(gen))
                if g.tzinfo is None:
                    g = g.replace(tzinfo=IST)
                news_age_min = round(
                    (now - g).total_seconds() / 60.0, 1)
                vix = ((read_state() or {}).get("market_tape")
                       or {}).get("vix")
        except Exception:
            pass
        msg = (f"pre-open warmup: universe {len(symbols)} symbols, "
               f"warmed 5m candles for {warmed}/"
               f"{min(WARM_SYMBOLS, len(symbols))}")
        if news_age_min is not None:
            msg += f", news state {news_age_min:g} min old"
        log.info(msg)
        activity.add("warmup", msg, universe=len(symbols), warmed=warmed,
                     news_age_min=news_age_min, vix=vix)

    # ── eod: exits summary + today's paper entry/exit counts ────────────
    def _eod(self, now: datetime):
        today = now.date().isoformat()
        summ: dict = {}
        try:
            ex = getattr(self.scanner, "exits", None)
            if ex is not None:
                s = ex.summary() or {}
                summ = {k: v for k, v in s.items()
                        if k != "positions"}        # rows, not blobs
        except Exception as e:
            log.warning("eod exits.summary failed: %s", e)
        entries = exits_n = 0
        try:
            for row in (self.scanner.paper.tail(500) or []):
                if str(row.get("logged_at") or "")[:10] != today:
                    continue
                t = row.get("type")
                if t == "paper_entry":
                    entries += 1
                elif t == "paper_exit":
                    exits_n += 1
        except Exception as e:
            log.warning("eod paper tail failed: %s", e)
        record = {"type": "eod_summary", "date": today,
                  "entries_today": entries, "exits_today": exits_n, **summ}
        try:
            self.scanner.paper.record(dict(record))
        except Exception as e:
            log.warning("eod record failed: %s", e)
        msg = (f"EOD {today}: {entries} entries, {exits_n} exits, "
               f"open {summ.get('open', 0)}, partial "
               f"{summ.get('partial', 0)}, closed today "
               f"{summ.get('closed_today', 0)}, pnl/share "
               f"{summ.get('realized_pnl_today_per_share_weighted', 0)}")
        log.info(msg)
        activity.add("eod", msg, **record)

    # ── loop plumbing ────────────────────────────────────────────────────
    def run_forever(self):
        log.info("orchestrator started (tick %ss, IST)", self.tick_seconds)
        activity.add("orchestrator",
                     f"orchestrator started (tick {self.tick_seconds}s)")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                  # belt AND braces
                self.last_error = str(e)
                log.warning("tick crashed: %s", e)
            self._stop.wait(self.tick_seconds)

    def start(self):
        threading.Thread(target=self.run_forever, name="orchestrator",
                         daemon=True).start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        try:
            ks = bool(is_paused())
        except Exception:
            ks = False
        return {"phase": self.phase, "last_tick": self.last_tick,
                "last_error": self.last_error, "killswitch": ks,
                "scanner_paused": bool(getattr(self.scanner, "paused",
                                               False)),
                "warmup_day": str(self._warmup_day or ""),
                "eod_day": str(self._eod_day or "")}


# ── self-test (offline: fake scanner/hub, injected clock) ───────────────
if __name__ == "__main__":
    import tempfile
    import time as _time
    from pathlib import Path

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    import core.killswitch as ks
    from core.activity import activity as _act

    tmp = Path(tempfile.mkdtemp(prefix="orch_selftest_"))
    ks.FLAG_PATH = tmp / "killswitch.flag"          # keep real flag untouched

    class FakeHub:
        def __init__(self):
            self.uni_calls = 0
            self.candle_calls = []

        def fo_universe(self):
            self.uni_calls += 1
            return [f"SYM{i}" for i in range(25)]

        def candles_5m(self, symbol, days=7):
            self.candle_calls.append(symbol)
            return object()                          # non-None "df"

    class FakePaper:
        def __init__(self):
            self.rows = [
                {"type": "paper_entry", "logged_at": "2026-07-03T10:05:00"},
                {"type": "paper_exit", "logged_at": "2026-07-03T11:00:00"},
                {"type": "paper_exit", "logged_at": "2026-07-03T12:00:00"},
                {"type": "paper_exit", "logged_at": "2026-07-01T12:00:00"},
            ]

        def record(self, entry):
            self.rows.append(dict(entry))

        def tail(self, n=200):
            return self.rows[-n:]

    class FakeExits:
        def summary(self):
            return {"open": 1, "partial": 0, "closed_today": 2,
                    "realized_pnl_today_per_share_weighted": 3.5,
                    "positions": [{"id": "huge-blob"}]}

    class FakeScanner:
        def __init__(self):
            self.paused = False
            self.exits = FakeExits()
            self.paper = FakePaper()
            self.universe = ["A", "B"]

    hub, sc = FakeHub(), FakeScanner()
    clock = {"now": datetime(2026, 7, 3, 8, 0, tzinfo=IST)}   # Friday
    orch = Orchestrator(sc, hub, now_fn=lambda: clock["now"])

    def at(day, h, m):
        clock["now"] = datetime(2026, 7, day, h, m, tzinfo=IST)
        orch.tick()

    def eod_rows():
        return [r for r in sc.paper.rows if r.get("type") == "eod_summary"]

    at(3, 8, 0)                                     # night -> closed
    assert orch.phase == "closed" and sc.paused is True

    at(3, 8, 55)                                    # pre_open: warmup once
    assert orch.phase == "pre_open"
    assert hub.uni_calls == 1 and len(hub.candle_calls) == 10
    assert sc.paused is True                        # not open yet
    at(3, 9, 0)                                     # still pre_open: no rerun
    assert hub.uni_calls == 1 and len(hub.candle_calls) == 10

    at(3, 9, 20)                                    # open, no kill-switch
    assert orch.phase == "open" and sc.paused is False

    ks.set_paused(True, source="test")              # UI hits pause
    at(3, 9, 25)
    assert sc.paused is True and ks.is_paused() is True
    sc.paused = False                               # drift: file must win
    at(3, 9, 26)
    assert sc.paused is True, "kill-switch must rule during hours"
    ks.set_paused(False, source="test")
    at(3, 9, 30)
    assert sc.paused is False and not ks.FLAG_PATH.exists()

    at(3, 15, 31)                                   # after close, before eod
    assert orch.phase == "closed" and sc.paused is True
    assert not ks.is_paused(), "phase pause must not touch the kill-switch"
    assert eod_rows() == []

    at(3, 15, 36)                                   # eod fires once
    rows = eod_rows()
    assert len(rows) == 1, rows
    eod = rows[0]
    assert eod["date"] == "2026-07-03"
    assert eod["entries_today"] == 1 and eod["exits_today"] == 2
    assert eod["open"] == 1 and eod["closed_today"] == 2
    assert "positions" not in eod, "positions blob must be stripped"
    at(3, 15, 40)                                   # same day: no second eod
    at(3, 16, 30)
    assert len(eod_rows()) == 1

    at(4, 8, 55)                                    # Saturday: no warmup
    assert orch.phase == "closed" and hub.uni_calls == 1
    at(4, 15, 40)                                   # Saturday: no eod
    assert len(eod_rows()) == 1

    at(6, 8, 55)                                    # Monday: warmup again
    assert orch.phase == "pre_open" and hub.uni_calls == 2
    assert len(hub.candle_calls) == 20
    at(6, 15, 36)                                   # Monday: second eod
    assert len(eod_rows()) == 2
    assert eod_rows()[-1]["date"] == "2026-07-06"
    assert eod_rows()[-1]["entries_today"] == 0

    # phase transitions + warmup + eod all landed in the journal
    kinds = [e["kind"] for e in _act.recent(300)]
    texts = [e["text"] for e in _act.recent(300) if e["kind"] == "phase"]
    assert kinds.count("warmup") == 2 and kinds.count("eod") == 2, kinds
    assert any("closed -> pre_open" in t for t in texts), texts
    assert any("pre_open -> open" in t for t in texts), texts
    assert any("open -> closed" in t for t in texts), texts
    assert "pause" in kinds and "killswitch" in kinds

    # a broken hub never kills the tick
    hub.fo_universe = lambda: 1 / 0
    at(7, 8, 55)                                    # Tuesday warmup: guarded
    assert orch.phase == "pre_open" and orch.last_tick

    # live loop smoke: daemon thread ticks with the injected clock
    orch2 = Orchestrator(FakeScanner(), FakeHub(),
                         now_fn=lambda: clock["now"], tick_seconds=0.05)
    orch2.start()
    _time.sleep(0.3)
    orch2.stop()
    assert orch2.last_tick is not None and orch2.phase is not None
    st = orch.status()
    assert st["phase"] == "pre_open" and st["killswitch"] is False

    print("orchestrator self-test OK:",
          {"phases_logged": kinds.count("phase"),
           "warmups": kinds.count("warmup"), "eods": kinds.count("eod"),
           "status": st})
