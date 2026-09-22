"""Scanner: sweeps the F&O universe on 5-minute bars, runs the OBS+Supertrend
trigger, scores candidates through the quant agent tree, and paper-logs
accepted signals. Execution happens in the ExitEngine: always the paper
book; real Shoonya orders too when core.trade_mode is LIVE and a
LiveExecutor is attached (engine/executor.py). The mode never flips
silently — only the UI toggle / POST /api/mode with typed confirmation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime, timedelta

LAST_ENTRY_TIME = dtime(15, 15)   # skip signal bars at/after 15:15 IST
SIGNAL_COOLDOWN = timedelta(minutes=45)   # per symbol+side, live sweeps

from quant.base import BUY, SELL
from quant.context import MarketContext
from signals.engine import check_signal

try:                                   # training-sample tap (fail-silent)
    from quant.training.collector import record as _training_record
except Exception:                      # keep the scanner importable without it
    def _training_record(result, source):
        pass

try:
    from core.activity import activity
except Exception:                      # keep scanner importable in tests
    class _NoopActivity:
        def add(self, *args, **kwargs):
            pass
    activity = _NoopActivity()

try:                                   # pure day-walk for the simulator;
    from engine import exits as _sim_exits    # optional — sim degrades to
except Exception:                              # entries-only without it
    _sim_exits = None

log = logging.getLogger("scanner")

try:
    from core import trade_mode
except Exception:                      # keep scanner importable in tests
    class _PaperOnly:
        @staticmethod
        def mode():
            return "paper"
    trade_mode = _PaperOnly()

# Gatekeeper agents: structurally-critical checks that VETO a signal when
# they score below the floor, no matter what the weighted composite says.
# Overridable per agent via quant_config.json agents.<key>.veto_below.
DEFAULT_VETOES = {"smc_structure": 25.0}
WALL_PROX_ATR = 0.6    # no BUY within this many ATRs below the call wall,
                       # no SELL within it above the put wall (user rule)
PAPER_LOG = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "paper_trades.jsonl")


def _sim_empty_stats() -> dict:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None,
            "pnl_sum_per_share": 0.0, "avg_win": None, "avg_loss": None,
            "best": None, "worst": None}


def _sim_stats(pnls: list[float]) -> dict:
    """Aggregate stats over the simulated (accepted + walked) trade P&Ls."""
    if not pnls:
        return _sim_empty_stats()
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    return {"trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate": round(100.0 * len(wins) / n, 1),
            "pnl_sum_per_share": round(sum(pnls), 4),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
            "best": round(max(pnls), 4), "worst": round(min(pnls), 4)}


class PaperBook:
    """Append-only log of accepted signals with their full score tree."""

    def __init__(self, path: str = PAPER_LOG):
        self.path = path
        self._lock = threading.Lock()

    def record(self, entry: dict):
        entry["logged_at"] = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def tail(self, n: int = 200) -> list[dict]:
        try:
            with open(self.path) as f:
                lines = f.readlines()[-n:]
            return [json.loads(x) for x in lines if x.strip()]
        except OSError:
            return []


class Scanner:
    def __init__(self, hub, root_agent, config, universe_limit: int = 0,
                 poll_seconds: int = 60):
        self.hub = hub
        self.root = root_agent
        self.config = config
        self.poll_seconds = poll_seconds
        self.paper = PaperBook()
        self.exits = None          # ExitEngine, injected by run_app
        self.paused = False        # kill switch: blocks NEW entries only;
                                   # exit management keeps running
        self._stop = threading.Event()

        symbols = hub.fo_universe()
        if universe_limit and universe_limit > 0:
            symbols = symbols[:universe_limit]
        self.universe: list[str] = symbols

        # UI state
        self.signals: list[dict] = []          # newest first, capped
        self.last_sweep: str | None = None
        self.sweep_seconds: float = 0.0
        self._sweep_count: int = 0             # for periodic activity rows
        self._seen: set[tuple] = set()         # (symbol, direction, bar_time)
        self._last_fired: dict[tuple, datetime] = {}   # cooldown tracking
        self.sim_status: dict = {"running": False, "day": None, "done": 0,
                                 "total": 0, "found": 0, "error": None,
                                 "stats": _sim_empty_stats(), "trades": []}

    # ── evaluation ──────────────────────────────────────────────────────
    def evaluate(self, symbol: str, direction: str,
                 df=None, price: float | None = None,
                 kind: str = "label", strategy: str = "intraday",
                 segment: str | None = None) -> dict | None:
        """Run the quant tree for symbol/direction. Used by the sweep AND
        by the UI's on-demand evaluate button. All signals are label
        events (v4: label arms, supertrend confirms) — `kind` is carried
        through for display only. `strategy`/`segment` tag the result for
        attribution (intraday vs swing vs manual; Cash/F&O/MCX)."""
        seg = segment or self.hub.segment_of(symbol)
        df = df if df is not None else self.hub.candles_5m(symbol, segment=seg)
        if df is None or df.empty:
            return None
        spot = price
        if spot is None:
            q = self.hub.cash_quote(symbol, segment=seg)
            spot = float(q["lp"]) if q and q.get("lp") else float(df["close"].iloc[-1])
        ctx = MarketContext(self.hub, symbol, direction, df, spot)
        tree = self.root.evaluate(ctx, self.config)
        composite = tree.score

        # gatekeeper vetoes: a structurally-critical agent scoring below its
        # veto floor rejects the signal no matter what the average says
        vetoes = []
        stack = [tree]
        while stack:
            node = stack.pop()
            vb = self.config.veto_below(node.key, DEFAULT_VETOES.get(node.key))
            if (vb is not None and node.enabled and node.available
                    and node.score is not None and node.score < vb):
                vetoes.append({"key": node.key, "score": node.score,
                               "floor": vb})
            stack.extend(node.children)

        # OI-wall proximity veto (user rule): never BUY into the call wall
        # (resistance) or SELL into the put wall (support) — markets revert
        # off those levels; entering INTO one is asking to be the liquidity.
        try:
            chain = ctx.chain
            atr_v = ctx.atr14
            if chain and chain.strikes and atr_v:
                def _wall(side):
                    best, oi = None, 0.0
                    for r in chain.strikes:
                        leg = getattr(r, side, None)
                        if leg and (leg.oi or 0) > oi:
                            best, oi = float(r.strike), leg.oi
                    return best
                if direction == BUY:
                    cw = _wall("ce")
                    if cw and cw > spot:
                        d = (cw - spot) / atr_v
                        if d < WALL_PROX_ATR:
                            vetoes.append({"key": "call_wall_proximity",
                                           "score": round(d, 2),
                                           "floor": WALL_PROX_ATR})
                else:
                    pw = _wall("pe")
                    if pw and pw < spot:
                        d = (spot - pw) / atr_v
                        if d < WALL_PROX_ATR:
                            vetoes.append({"key": "put_wall_proximity",
                                           "score": round(d, 2),
                                           "floor": WALL_PROX_ATR})
        except Exception:
            pass

        accepted = (composite is not None
                    and composite >= self.config.signal_threshold
                    and not vetoes)
        result = {"symbol": symbol, "direction": direction, "price": spot,
                  "score": composite, "accepted": accepted,
                  "threshold": self.config.signal_threshold,
                  "strategy": strategy, "segment": seg,
                  "tree": tree.to_dict(),
                  "evaluated_at": datetime.now().isoformat(timespec="seconds")}
        if vetoes:
            result["vetoed_by"] = vetoes
        return result

    # ── the sweep ───────────────────────────────────────────────────────
    def sweep_once(self):
        t0 = time.monotonic()
        hits = 0
        accepted = 0
        # paused = kill switch: skip the entry scan entirely, but fall
        # through so the exit engine still manages open positions below
        for symbol in (self.universe if not self.paused else []):
            if self._stop.is_set():
                break
            try:
                df = self.hub.candles_5m(symbol)
                cand = check_signal(symbol, df)
                if not cand:
                    continue
                # no fresh entries in the final minutes of the session —
                # nothing sane can be done with a 15:20+ signal
                if cand.bar_time.time() >= LAST_ENTRY_TIME:
                    continue
                key = (cand.symbol, cand.direction, cand.bar_time.isoformat())
                if key in self._seen:
                    continue                    # one alert per bar per side
                # cooldown: a fresh label on the same symbol+side within
                # SIGNAL_COOLDOWN of the last one is a repeat, not a new idea
                last = self._last_fired.get((cand.symbol, cand.direction))
                if last and (cand.bar_time - last) < SIGNAL_COOLDOWN:
                    continue
                self._seen.add(key)
                self._last_fired[(cand.symbol, cand.direction)] = cand.bar_time
                result = self.evaluate(symbol, cand.direction,
                                       df=df, price=cand.price,
                                       kind=cand.kind)
                if not result:
                    continue
                result["trigger"] = cand.to_dict()
                _training_record(result, "intraday")   # training-sample tap
                self.signals.insert(0, result)
                del self.signals[300:]
                hits += 1
                if result["accepted"]:
                    accepted += 1
                    self.paper.record({"type": "paper_entry", **result})
                    log.info("PAPER %s %s @ %.2f score=%.1f",
                             cand.direction, symbol, cand.price,
                             result["score"] or -1)
                    activity.add(
                        "signal",
                        f"{cand.direction} {symbol} @ {cand.price:g} "
                        f"score {result['score']:.1f}",
                        symbol=symbol, direction=cand.direction,
                        price=float(cand.price), score=result["score"])
                    if self.exits:
                        try:
                            self.exits.open_from_signal(result)
                        except Exception as e:
                            log.warning("exit engine open %s failed: %s",
                                        symbol, e)
            except Exception as e:
                log.debug("sweep %s failed: %s", symbol, e)
        self.sweep_seconds = round(time.monotonic() - t0, 1)
        self.last_sweep = datetime.now().isoformat(timespec="seconds")
        self._sweep_count += 1
        if hits:
            log.info("sweep done in %.1fs — %d new candidates",
                     self.sweep_seconds, hits)
        if hits or self._sweep_count % 10 == 1:   # hits, or every ~10th
            swept = 0 if self.paused else len(self.universe)
            activity.add(
                "sweep",
                f"swept {swept} symbols in {self.sweep_seconds}s — "
                f"{hits} new candidates, {accepted} accepted",
                swept=swept, hits=hits, accepted=accepted,
                seconds=self.sweep_seconds, paused=self.paused)
        if self.exits:                 # runs even when paused (kill switch)
            try:
                self.exits.update_all()
            except Exception as e:
                log.warning("exit engine update failed: %s", e)

    # ── day simulation (replay the last session through the live pipeline) ─
    def simulate_day(self, limit: int = 0):
        """Replay the most recent session bar-by-bar: OBS label events +
        Supertrend gate exactly as live, full quant-tree scoring for every
        candidate. Results land in the signal feed flagged sim=True.

        Honesty note: candle-driven agents (SMC, VWAP, rel-volume, structure)
        see true point-in-time data (the candle prefix); chain/VIX/futures
        quotes are the CURRENT snapshot (≈ last close on a weekend) because
        historical option chains cannot be reconstructed. Good enough to
        validate the pipeline; not a backtest."""
        symbols = self.universe[:limit] if limit else self.universe
        self.sim_status = {"running": True, "day": None, "done": 0,
                           "total": len(symbols), "found": 0,
                           "error": None,
                           "stats": _sim_empty_stats(), "trades": []}
        # one batched yahoo download primes the cache for the whole universe
        # (seconds instead of minutes) and guarantees sim, chart and exit
        # walker all see the IDENTICAL candle frame per symbol
        try:
            if hasattr(self.hub, "prefetch_candles"):
                self.hub.prefetch_candles(symbols)
        except Exception as e:
            log.debug("prefetch failed: %s", e)
        fired: set[tuple] = set()      # one signal per (symbol, direction)
        sim_pnls: list[float] = []     # accepted + walked trades only
        try:
            for symbol in symbols:
                if self._stop.is_set():
                    break
                self.sim_status["done"] += 1
                try:
                    df = self.hub.candles_5m(symbol)
                    if df is None or len(df) < 80:
                        continue
                    day = df.index[-1].date()
                    self.sim_status["day"] = str(day)
                    # v5: SAME code path as live — day_signals() is the
                    # single source of truth (OBS bias + v2 pullback entry)
                    from signals.engine import day_signals
                    for s in day_signals(df):
                        i = s["bar"]
                        ts = df.index[i]
                        if ts.time() >= LAST_ENTRY_TIME:
                            continue
                        direction = s["direction"]
                        # the FIRST entry per symbol+side is the tradeable
                        # one — later repeats the same day are noise
                        if (symbol, direction) in fired:
                            continue
                        fired.add((symbol, direction))
                        prefix = df.iloc[:i + 1]
                        price = float(prefix["close"].iloc[-1])
                        result = self.evaluate(symbol, direction,
                                               df=prefix, price=price,
                                               kind=s["kind"])
                        if not result:
                            continue
                        result["sim"] = True
                        aj = s["armed_bar"]
                        if aj is not None:
                            armed_by = {"label": ("Look to buy"
                                                  if direction == BUY
                                                  else "Look to sell"),
                                        "bar_time": df.index[aj].isoformat()}
                        elif s["kind"] == "triangle":
                            armed_by = {"label": ("up triangle"
                                                  if direction == BUY
                                                  else "down triangle"),
                                        "bar_time": ts.isoformat()}
                        else:
                            armed_by = None
                        result["trigger"] = {
                            "symbol": symbol, "direction": direction,
                            "price": price, "bar_time": ts.isoformat(),
                            "st_bull": s["st_bull"], "kind": s["kind"],
                            "st_line": s["st_line"],
                            "armed_by": armed_by,
                            "obs": {"replay": True}}
                        # accepted -> walk the rest of the day (exits sim)
                        if result["accepted"] and _sim_exits is not None:
                            try:
                                walk = _sim_exits.simulate_position(
                                    df, i, direction, price)
                                result["sim_trade"] = walk
                                last = (walk["exits"][-1]
                                        if walk.get("exits") else {})
                                pnl = float(walk.get("pnl_per_share") or 0.0)
                                sim_pnls.append(pnl)
                                self.sim_status["trades"].append({
                                    "symbol": symbol,
                                    "direction": direction,
                                    "entry_time": ts.isoformat(),
                                    "entry": price,
                                    "exit_time": last.get("time"),
                                    "exit": last.get("price"),
                                    "exit_reason": last.get("reason"),
                                    "pnl_per_share": pnl,
                                    "score": result.get("score")})
                                self.sim_status["stats"] = \
                                    _sim_stats(sim_pnls)
                            except Exception as e:
                                log.debug("sim walk %s failed: %s",
                                          symbol, e)
                        self.signals.insert(0, result)
                        del self.signals[400:]
                        self.sim_status["found"] += 1
                except Exception as e:
                    log.debug("sim %s failed: %s", symbol, e)
        except Exception as e:
            self.sim_status["error"] = str(e)
        finally:
            self.sim_status["running"] = False
            log.info("simulation done: %s signals across %s symbols",
                     self.sim_status["found"], self.sim_status["done"])

    def start_simulation(self, limit: int = 0) -> bool:
        """Kick off simulate_day in the background; False if already running."""
        if getattr(self, "sim_status", {}).get("running"):
            return False
        threading.Thread(target=self.simulate_day, kwargs={"limit": limit},
                         name="sim-day", daemon=True).start()
        return True

    def run_forever(self):
        log.info("scanner started: %d symbols, poll %ds, mode=%s",
                 len(self.universe), self.poll_seconds, trade_mode.mode())
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception as e:
                log.warning("sweep crashed: %s", e)
            self._stop.wait(self.poll_seconds)

    def start(self):
        threading.Thread(target=self.run_forever, name="scanner",
                         daemon=True).start()

    def stop(self):
        self._stop.set()
