"""DataHub — all Shoonya data access for the scanner + agents, with caching
and polite rate limiting (sequential REST calls, small gaps, TTL caches).

Instrument resolution comes from the daily scrip masters:
  NSE master -> cash token for "SYMBOL-EQ"
  NFO master -> nearest stock future (FUTSTK) and monthly option (OPTSTK)
                contracts per underlying
"""

from __future__ import annotations

import calendar
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

from shoonya_client import ShoonyaSession, load_scripmaster
from .context import ChainSnapshot, OptionLeg, StrikeRow

log = logging.getLogger("datahub")

REQUEST_GAP = 0.06        # ~16 req/s ceiling, stays under broker limits
MIN_BARS_PER_SESSION = 45  # a non-today session with fewer 5m bars than this
                           # is INCOMPLETE data (full day = 75) → warn (no fallback)
BREAKER_FAILS = 3          # consecutive Shoonya failures that open the breaker
BREAKER_HOLD = 300.0       # seconds the breaker stays open (5 min)
CHAIN_TTL = 90.0          # option-chain snapshot reuse window (s)
QUOTE_TTL = 5.0
CANDLE_TTL = 60.0
STRIKE_SPAN = 8           # strikes each side of ATM


def _parse_expiry(raw: str) -> float | None:
    """NFO master Expiry like '30-JUL-2026' -> epoch of 15:30 IST that day."""
    try:
        d = datetime.strptime(raw.strip().title(), "%d-%b-%Y")
        return d.replace(hour=15, minute=30).timestamp()
    except ValueError:
        return None


class DataHub:
    def __init__(self, session: ShoonyaSession):
        self.session = session
        self._lock = threading.Lock()
        self._last_req = 0.0
        self._cache: dict[str, tuple[float, object]] = {}
        self.nse = load_scripmaster("NSE")
        self.nfo = load_scripmaster("NFO")
        self.mcx = self._load_master_safe("MCX")   # commodities (optional)
        self._nfo_by_symbol = self._index_nfo()
        self._mcx_by_symbol = self._index_mcx()
        self._vix_token = self._find_vix_token()
        self.feed = None          # ShoonyaFeed — attach_feed() for live LTP
        self.tick_cache = None    # feed.cache.TickCache — freshness-aware push cache
        self.candle_source: dict[str, str] = {}   # symbol -> shoonya
        # option-chain leg quotes are fetched concurrently (a bounded thread
        # pool) instead of ~34 serial REST round-trips per symbol. The pool
        # size caps in-flight load; tune on the VPS if Shoonya rate-limits.
        self._chain_workers = max(1, int(os.getenv("CHAIN_FETCH_WORKERS", "6")))
        # circuit breaker: when Shoonya REST is hard-down (nightly
        # maintenance / outages), stop burning 6s of retries per call and
        # jump straight to fallbacks for BREAKER_HOLD seconds
        self._shoonya_fails = 0
        self._shoonya_down_until = 0.0

    # ── plumbing ────────────────────────────────────────────────────────
    def _throttle(self):
        with self._lock:
            wait = REQUEST_GAP - (time.monotonic() - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.monotonic()

    def _cached(self, key: str, ttl: float, fetch):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = fetch()
        self._cache[key] = (time.time(), value)
        return value

    # ── Shoonya circuit breaker ─────────────────────────────────────────
    def shoonya_up(self) -> bool:
        return time.time() >= self._shoonya_down_until

    def _shoonya_result(self, ok: bool, what: str = "", err=None):
        if ok:
            if self._shoonya_fails >= BREAKER_FAILS:
                log.info("shoonya REST recovered — circuit closed")
            self._shoonya_fails = 0
            return
        self._shoonya_fails += 1
        if err is not None:
            self._last_shoonya_err = f"{type(err).__name__}: {err}"
        if (self._shoonya_fails == BREAKER_FAILS
                and self.shoonya_up()):
            self._shoonya_down_until = time.time() + BREAKER_HOLD
            log.warning("shoonya REST down (%s ×%d, last error: %s) — "
                        "circuit OPEN for %.0fs, using fallbacks directly",
                        what, self._shoonya_fails,
                        getattr(self, "_last_shoonya_err", "?"),
                        BREAKER_HOLD)
            try:
                from core.activity import activity
                activity.add("data", "Shoonya REST down — fallback mode "
                                     f"for {BREAKER_HOLD:.0f}s")
            except Exception:
                pass

    def _quote_raw(self, exchange: str, token: str,
                   throttle: bool = True) -> dict | None:
        """One quote: prefers a fresh push-cache tick, else TTL-cached REST.

        ``throttle`` applies the global inter-request gap on a cache miss (True
        for serial callers). The concurrent chain batch passes ``throttle=False``
        so the thread-pool size — not the 0.06s gap — bounds in-flight load.
        """
        if not self.shoonya_up():
            return None                    # breaker open — fail fast
        cache = self.tick_cache
        if cache is not None:              # push cache first (no REST)
            ps = cache.get(f"{exchange}|{token}")
            if ps is not None and ps.lp > 0 and ps.is_fresh(QUOTE_TTL):
                return ps.as_quote()
        def fetch():
            if throttle:
                self._throttle()
            q = self.session.get_quote(exchange, token)
            return q if isinstance(q, dict) else None
        try:
            out = self._cached(f"q:{exchange}|{token}", QUOTE_TTL, fetch)
            self._shoonya_result(True)
            return out
        except Exception as e:
            # Only GENUINE outages count toward the circuit breaker. A per-
            # instrument reject (HTTP 400 / stat!=Ok — e.g. a stale option
            # token after an expiry roll) must NOT open the GLOBAL breaker, or
            # a handful of dead legs black out the whole hub (chain, GEX,
            # microstructure, evaluate). Terminal transient failures surface as
            # ShoonyaError "...giving up after N attempts..." (shoonya_client);
            # 5xx is server-side. Everything else is a benign per-token skip.
            log.debug("quote %s|%s failed: %s", exchange, token, e)
            status = getattr(getattr(e, "response", None), "status_code", None)
            transient = ("giving up after" in str(e)) or (status is not None
                                                          and status >= 500)
            if transient:
                self._shoonya_result(False, "GetQuotes", err=e)
            return None

    def _quote(self, exchange: str, token: str) -> dict | None:
        return self._quote_raw(exchange, token, throttle=True)

    def _quotes_batch(self, reqs: list[tuple[str, str]]) -> dict[str, dict]:
        """Fetch many quotes concurrently -> ``{token: quote}``.

        Fresh push-cache ticks are served without any REST call; the rest are
        fetched over a bounded thread pool (``CHAIN_FETCH_WORKERS``), collapsing
        the old ~34-serial-calls-per-symbol option-chain fetch into a few
        concurrent rounds. Honours the circuit breaker and per-token TTL cache.
        """
        out: dict[str, dict] = {}
        todo: list[tuple[str, str]] = []
        cache = self.tick_cache
        for exch, token in reqs:
            if not token:
                continue
            if cache is not None:
                ps = cache.get(f"{exch}|{token}")
                if ps is not None and ps.lp > 0 and ps.is_fresh(QUOTE_TTL):
                    out[token] = ps.as_quote()
                    continue
            todo.append((exch, token))
        if not todo or not self.shoonya_up():
            return out
        workers = min(self._chain_workers, len(todo))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="quote") as ex:
            futs = {ex.submit(self._quote_raw, e, t, False): t
                    for e, t in todo}
            for fut in as_completed(futs):
                try:
                    q = fut.result()
                except Exception:
                    q = None
                if q:
                    out[futs[fut]] = q
        return out

    # ── instrument resolution ───────────────────────────────────────────
    @staticmethod
    def _load_master_safe(exchange: str) -> dict:
        """Load a scrip master, returning {} on failure so an optional segment
        (e.g. MCX) never blocks DataHub init."""
        try:
            return load_scripmaster(exchange)
        except Exception as e:
            log.warning("scrip master %s unavailable (%s) — segment disabled",
                        exchange, e)
            return {}

    def _index_mcx(self) -> dict:
        """Group the MCX master by commodity Symbol -> expiry-sorted
        futures/options (FUT*/OPT* instruments, e.g. FUTCOM / OPTFUT)."""
        by_sym: dict[str, dict] = {}
        for row in self.mcx.values():
            sym = (row.get("Symbol") or "").strip()
            inst = (row.get("Instrument") or "").strip()
            if not sym or not (inst.startswith("FUT") or inst.startswith("OPT")):
                continue
            exp = _parse_expiry(row.get("Expiry", ""))
            if exp is None or exp < time.time():
                continue
            slot = by_sym.setdefault(sym, {"futures": [], "options": []})
            bucket = "futures" if inst.startswith("FUT") else "options"
            slot[bucket].append((exp, row))
        for slot in by_sym.values():
            slot["futures"].sort(key=lambda x: x[0])
            slot["options"].sort(key=lambda x: x[0])
        return by_sym

    def _index_nfo(self) -> dict:
        by_sym: dict[str, dict] = {}
        for row in self.nfo.values():
            sym = (row.get("Symbol") or "").strip()
            inst = (row.get("Instrument") or "").strip()
            if not sym or inst not in ("FUTSTK", "OPTSTK", "FUTIDX", "OPTIDX"):
                continue
            exp = _parse_expiry(row.get("Expiry", ""))
            if exp is None or exp < time.time():
                continue
            slot = by_sym.setdefault(sym, {"futures": [], "options": []})
            if inst.startswith("FUT"):
                slot["futures"].append((exp, row))
            else:
                slot["options"].append((exp, row))
        for slot in by_sym.values():
            slot["futures"].sort(key=lambda x: x[0])
            slot["options"].sort(key=lambda x: x[0])
        return by_sym

    @staticmethod
    def _is_real_symbol(sym: str) -> bool:
        """Shoonya's scrip master ships NSE TEST instruments (011NSETEST,
        021NSETEST, ...) — exchange plumbing, not tradeable stocks."""
        s = (sym or "").upper()
        return bool(s) and s[0].isalpha() and "NSETEST" not in s \
            and "TESTING" not in s

    def fo_universe(self) -> list[str]:
        """All stock symbols with live futures (the F&O stock list)."""
        return sorted(s for s, slot in self._nfo_by_symbol.items()
                      if self._is_real_symbol(s) and slot["futures"] and
                      slot["futures"][0][1].get("Instrument") == "FUTSTK")

    def cash_row(self, symbol: str) -> dict | None:
        return self.nse.get(f"{symbol}-EQ") or self.nse.get(symbol)

    def cash_token(self, symbol: str) -> str | None:
        row = self.cash_row(symbol)
        return row.get("Token") if row else None

    # ── multi-segment (Cash / F&O / MCX) ─────────────────────────────────
    def mcx_universe(self) -> list[str]:
        """MCX commodities with a live front-month future. Optional explicit
        allow-list via MCX_UNIVERSE (comma-separated); empty = all."""
        syms = sorted(s for s, slot in self._mcx_by_symbol.items()
                      if slot["futures"])
        allow = [x.strip().upper() for x in
                 os.getenv("MCX_UNIVERSE", "").split(",") if x.strip()]
        return [s for s in syms if s.upper() in allow] if allow else syms

    def cash_universe(self) -> list[str]:
        """NSE cash-equity scan list. Default = F&O underlyings' equities (a
        bounded, liquid set); override/extend via CASH_UNIVERSE (comma list)."""
        allow = [x.strip().upper() for x in
                 os.getenv("CASH_UNIVERSE", "").split(",") if x.strip()]
        if allow:
            return [s for s in allow if self.cash_token(s)]
        return self.fo_universe()

    def segment_of(self, symbol: str) -> str:
        """Best-effort segment for a bare symbol: MCX (commodity future) >
        FNO (has NFO futures) > CASH (plain NSE equity)."""
        slot = self._mcx_by_symbol.get(symbol)
        if slot and slot["futures"]:
            return "MCX"
        slot = self._nfo_by_symbol.get(symbol)
        if slot and slot["futures"]:
            return "FNO"
        return "CASH"

    def candle_ref(self, symbol: str, segment: str | None = None
                   ) -> tuple[str, str | None]:
        """(exchange, token) for OHLCV candles of `symbol`: NSE cash for
        CASH/FNO underlyings, MCX front-month future for commodities."""
        seg = segment or self.segment_of(symbol)
        if seg == "MCX":
            slot = self._mcx_by_symbol.get(symbol)
            tok = slot["futures"][0][1].get("Token") if slot and slot["futures"] else None
            return "MCX", tok
        return "NSE", self.cash_token(symbol)

    def _find_vix_token(self) -> str | None:
        for tsym, row in self.nse.items():
            name = (row.get("Symbol") or "") + " " + tsym
            if "INDIAVIX" in name.replace(" ", "").upper():
                return row.get("Token")
        return None

    # ── market data views ───────────────────────────────────────────────
    @staticmethod
    def _candles_healthy(df) -> bool:
        """Quality gate for the candle series. Shoonya's TPSeries sometimes
        serves PARTIAL history (a whole afternoon missing) — the lost move
        then reappears as one monster gap bar at the next open, and every
        oscillator fires garbage signals in the first bars of the day
        (the TATASTEEL 09:15 cluster). A session that isn't today must have
        a reasonably complete bar count."""
        if df is None or len(df) < 80:
            return False
        try:
            today = datetime.now().date()
            dates = pd.Series(df.index.date, index=df.index)
            sessions = list(dict.fromkeys(dates))          # ordered unique
            checked = 0
            for d in reversed(sessions):
                if d == today:
                    continue                    # partial by nature intraday
                if int((dates == d).sum()) < MIN_BARS_PER_SESSION:
                    return False
                checked += 1
                if checked >= 2:                # last two full sessions
                    break
            return checked > 0
        except Exception:
            return True                          # never block on the checker

    def prefetch_candles(self, symbols: list, batch: int = 40) -> int:
        """Warm the 5m candle cache for many symbols via CONCURRENT Shoonya
        TPSeries fetches (replaces the old single yfinance batch download).
        Pool size bounds in-flight requests; each result is cached so the live
        sweep finds a warm entry."""
        def ckey(s):
            exch, _ = self.candle_ref(s)
            return f"c5:{exch}:{s}"
        todo = [s for s in symbols
                if not (self._cache.get(ckey(s))
                        and time.time() - self._cache[ckey(s)][0] < CANDLE_TTL)]
        if not todo:
            return 0
        loaded = 0
        workers = max(1, min(batch, self._chain_workers, len(todo)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="candles") as ex:
            futs = {ex.submit(self._fetch_candles, s, "5", 7, None, False): s
                    for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    df = fut.result()
                except Exception:
                    df = None
                if df is not None and not df.empty:
                    self._cache[ckey(s)] = (time.time(), df)
                    loaded += 1
        if loaded:
            log.info("prefetched %d/%d symbols via shoonya", loaded, len(todo))
        return loaded

    def _fetch_candles(self, symbol: str, interval: str = "5", days: int = 7,
                       segment: str | None = None,
                       throttle: bool = True) -> pd.DataFrame | None:
        """Raw OHLCV from Shoonya TPSeries for any segment/interval (no TTL
        cache). Exchange+token resolve via :meth:`candle_ref` (NSE cash for
        Cash/F&O underlyings, MCX front-month future for commodities).

        ``throttle`` applies the global inter-request gap (True for serial
        callers; the concurrent prefetch pool passes False so pool size bounds
        load).
        """
        exch, token = self.candle_ref(symbol, segment)
        if not token or not self.shoonya_up():
            return None
        if throttle:
            self._throttle()
        start = time.time() - days * 86400
        try:
            rows = self.session.get_time_series(exch, token, start,
                                                time.time(), interval=str(interval))
            self._shoonya_result(rows is not None, "TPSeries")
        except Exception as e:
            log.debug("shoonya candles %s (%s %sm) failed: %s",
                      symbol, exch, interval, e)
            self._shoonya_result(False, "TPSeries", err=e)
            return None
        if not rows:
            return None
        recs = []
        for r in rows:
            try:
                recs.append({
                    "time": datetime.strptime(r["time"], "%d-%m-%Y %H:%M:%S"),
                    "open": float(r["into"]), "high": float(r["inth"]),
                    "low": float(r["intl"]), "close": float(r["intc"]),
                    "volume": float(r.get("intv") or 0),
                    "oi": float(r.get("intoi") or 0),
                })
            except (KeyError, ValueError):
                continue
        if not recs:
            return None
        df = pd.DataFrame(recs).set_index("time").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        self.candle_source[symbol] = "shoonya"
        # the partial-history gate is 5m-session specific; skip for other TFs
        if str(interval) == "5" and not self._candles_healthy(df):
            # Shoonya sometimes serves partial history. With yfinance removed
            # there is NO fallback source — warn (loudly in LIVE) but still
            # return the best-available bars so exits/logic keep running.
            log.warning("candles %s: Shoonya history looks incomplete "
                        "(%d bars) — no fallback source", symbol, len(df))
            try:
                from core import trade_mode
                if trade_mode.is_live():
                    from core.activity import activity
                    activity.add("data",
                                 f"LIVE candles {symbol}: Shoonya history "
                                 "incomplete — no fallback; verify the feed",
                                 symbol=symbol)
            except Exception:
                pass
        return df

    def candles_5m(self, symbol: str, days: int = 7,
                   segment: str | None = None) -> pd.DataFrame | None:
        exch, _ = self.candle_ref(symbol, segment)
        try:
            return self._cached(
                f"c5:{exch}:{symbol}", CANDLE_TTL,
                lambda: self._fetch_candles(symbol, "5", days, segment, True))
        except Exception as e:
            log.debug("candles %s failed: %s", symbol, e)
            return None

    def candles(self, symbol: str, interval: str = "5", days: int = 7,
                segment: str | None = None,
                ttl: float | None = None) -> pd.DataFrame | None:
        """General intraday OHLCV for any interval/segment (used by the swing
        scanner: 15m / 60m). Daily bars come from :meth:`daily_candles`."""
        exch, _ = self.candle_ref(symbol, segment)
        ttl = CANDLE_TTL if ttl is None else ttl
        try:
            return self._cached(
                f"cx:{exch}:{symbol}:{interval}:{days}", ttl,
                lambda: self._fetch_candles(symbol, interval, days, segment, True))
        except Exception as e:
            log.debug("candles %s %s failed: %s", symbol, interval, e)
            return None

    def daily_candles(self, symbol: str, days: int = 40,
                      segment: str | None = None) -> pd.DataFrame | None:
        if not self.shoonya_up():
            return None                    # breaker open — fail fast
        seg = segment or self.segment_of(symbol)
        if seg == "MCX":
            slot = self._mcx_by_symbol.get(symbol)
            if not slot or not slot["futures"]:
                return None
            exch, sym_arg = "MCX", slot["futures"][0][1].get("TradingSymbol", "")
        else:
            exch, sym_arg = "NSE", f"{symbol}-EQ"
        def fetch():
            self._throttle()
            end = time.time()
            rows = self.session.get_daily_history(
                exch, sym_arg, end - days * 86400, end)
            if not rows:
                return None
            recs = []
            for r in rows:
                try:
                    recs.append({
                        "time": pd.to_datetime(r.get("time"),
                                               format="%d-%b-%Y", errors="coerce"),
                        "open": float(r["into"]), "high": float(r["inth"]),
                        "low": float(r["intl"]), "close": float(r["intc"]),
                        "volume": float(r.get("intv") or 0)})
                except (KeyError, ValueError, TypeError):
                    continue
            recs = [x for x in recs if pd.notna(x["time"])]
            if not recs:
                return None
            return pd.DataFrame(recs).set_index("time").sort_index()
        try:
            return self._cached(f"d:{exch}:{symbol}", 3600.0, fetch)
        except Exception as e:
            log.debug("daily %s failed: %s", symbol, e)
            return None

    # ── live websocket feed (attach once from run_app) ───────────────────
    FEED_FRESH_SECONDS = 30.0     # ws snapshot older than this -> REST

    def attach_feed(self, feed, cache=None):
        """Wire the ShoonyaFeed so cash_quote()/india_vix()/futures_quote()
        serve real-time websocket ticks (REST only as fallback).

        ``cache`` is the optional freshness-aware :class:`feed.cache.TickCache`
        populated by :class:`feed.pump.FeedPump`; when present it is preferred
        over the raw ``feed.quotes`` dict because its freshness is measured on a
        monotonic clock (immune to wall-clock jumps and to a missing ``ft``).
        """
        self.feed = feed
        self.tick_cache = cache

    def _feed_quote(self, exchange: str, token: str) -> dict | None:
        """Live snapshot from the push feed, if fresh enough (else None -> REST)."""
        if not token:
            return None
        key = f"{exchange}|{token}"
        cache = self.tick_cache
        if cache is not None:             # monotonic-fresh push cache
            ps = cache.get(key)
            if ps is not None and ps.lp > 0 and ps.is_fresh(self.FEED_FRESH_SECONDS):
                return ps.as_quote()
        feed = self.feed                  # legacy path: raw merged dict + ft gate
        if feed is None:
            return None
        try:
            snap = feed.quotes.get(key)
            if not snap or not snap.get("lp"):
                return None
            ft = float(snap.get("ft") or 0)
            if ft and (time.time() - ft) > self.FEED_FRESH_SECONDS:
                return None               # stale (halt/disconnect) -> REST
            return snap
        except Exception:
            return None

    def feed_status(self) -> dict:
        """For the UI: is the websocket connected and how many live quotes."""
        feed = self.feed
        if feed is None:
            return {"attached": False, "connected": False, "instruments": 0}
        try:
            return {"attached": True,
                    "connected": bool(feed._connected.is_set()),
                    "instruments": len(feed.quotes)}
        except Exception:
            return {"attached": True, "connected": False, "instruments": 0}

    def cash_quote(self, symbol: str, segment: str | None = None) -> dict | None:
        """Underlying spot quote. For MCX (no cash leg) this is the front-month
        future — the natural 'spot' proxy for commodities."""
        seg = segment or self.segment_of(symbol)
        if seg == "MCX":
            return self.futures_quote(symbol, segment="MCX")
        token = self.cash_token(symbol)
        if not token:
            return None
        live = self._feed_quote("NSE", token)
        if live is not None:
            return live
        return self._quote("NSE", token)

    def price(self, symbol: str, segment: str | None = None) -> float | None:
        """Last price (float) for any segment — Cash/F&O spot or MCX future.
        Convenience for the OI scanner, watchlist P&L and manual orders."""
        q = self.cash_quote(symbol, segment)
        try:
            return float(q["lp"]) if q and q.get("lp") else None
        except (ValueError, TypeError):
            return None

    def india_vix(self) -> float | None:
        if not self._vix_token:
            return None
        q = self._quote("NSE", self._vix_token)
        try:
            return float(q["lp"]) if q and q.get("lp") else None
        except (ValueError, TypeError):
            return None

    def futures_quote(self, symbol: str,
                      segment: str | None = None) -> dict | None:
        seg = segment or self.segment_of(symbol)
        idx, exch = ((self._mcx_by_symbol, "MCX") if seg == "MCX"
                     else (self._nfo_by_symbol, "NFO"))
        slot = idx.get(symbol)
        if not slot or not slot["futures"]:
            return None
        _, row = slot["futures"][0]
        q = self._quote(exch, row.get("Token"))
        if not q:
            return None
        q["lot"] = int(float(row.get("LotSize") or 0) or 0)
        q["expiry_epoch"] = _parse_expiry(row.get("Expiry", ""))
        q["exchange"] = exch
        q["tsym"] = row.get("TradingSymbol", "")
        return q

    def futures_candles(self, symbol: str, interval: str = "5", days: int = 5,
                        segment: str | None = None) -> pd.DataFrame | None:
        """Front-month FUTURE intraday candles — these carry Open Interest
        (``oi`` column) which cash-equity candles do not. Used by the OI
        scanner. F&O -> NFO future, commodities -> MCX future."""
        seg = segment or self.segment_of(symbol)
        idx, exch = ((self._mcx_by_symbol, "MCX") if seg == "MCX"
                     else (self._nfo_by_symbol, "NFO"))
        slot = idx.get(symbol)
        if not slot or not slot["futures"]:
            return None
        token = slot["futures"][0][1].get("Token")
        if not token:
            return None

        def fetch():
            if not self.shoonya_up():
                return None
            self._throttle()
            start = time.time() - days * 86400
            try:
                rows = self.session.get_time_series(exch, token, start,
                                                    time.time(), interval=str(interval))
                self._shoonya_result(rows is not None, "TPSeries")
            except Exception as e:
                log.debug("futures candles %s failed: %s", symbol, e)
                self._shoonya_result(False, "TPSeries", err=e)
                return None
            if not rows:
                return None
            recs = []
            for r in rows:
                try:
                    recs.append({
                        "time": datetime.strptime(r["time"], "%d-%m-%Y %H:%M:%S"),
                        "open": float(r["into"]), "high": float(r["inth"]),
                        "low": float(r["intl"]), "close": float(r["intc"]),
                        "volume": float(r.get("intv") or 0),
                        "oi": float(r.get("intoi") or 0)})
                except (KeyError, ValueError):
                    continue
            if not recs:
                return None
            df = pd.DataFrame(recs).set_index("time").sort_index()
            return df[~df.index.duplicated(keep="last")]
        try:
            return self._cached(f"fc:{exch}:{symbol}:{interval}", CANDLE_TTL, fetch)
        except Exception as e:
            log.debug("futures candles %s failed: %s", symbol, e)
            return None

    # ── option chain snapshot ───────────────────────────────────────────
    def chain_snapshot(self, symbol: str,
                       span: int = STRIKE_SPAN) -> ChainSnapshot | None:
        if not self.shoonya_up():
            return None                    # breaker open — 30+ quote calls
                                           # would burn minutes of retries
        def fetch():
            slot = self._nfo_by_symbol.get(symbol)
            if not slot or not slot["options"]:
                return None
            near_exp = slot["options"][0][0]
            monthly = [(e, r) for e, r in slot["options"]
                       if abs(e - near_exp) < 86400 * 40]
            rows = [r for e, r in monthly if abs(e - near_exp) < 1.0]
            if not rows:
                return None
            cq = self.cash_quote(symbol)
            spot = float(cq["lp"]) if cq and cq.get("lp") else None
            if not spot:
                return None
            lot = int(float(rows[0].get("LotSize") or 0) or 0)

            by_strike: dict[float, dict] = {}
            for r in rows:
                try:
                    k = float(r.get("StrikePrice") or 0)
                except ValueError:
                    continue
                if k <= 0:
                    continue
                by_strike.setdefault(k, {})[
                    (r.get("OptionType") or "").strip()] = r
            strikes_sorted = sorted(by_strike)
            if not strikes_sorted:
                return None
            atm_i = min(range(len(strikes_sorted)),
                        key=lambda i: abs(strikes_sorted[i] - spot))
            window = strikes_sorted[max(0, atm_i - span):
                                    atm_i + span + 1]

            snap = ChainSnapshot(symbol=symbol, expiry_epoch=near_exp,
                                 lot=lot, spot=spot)
            # Collect every leg in the window, then fetch ALL their quotes
            # concurrently (was ~34 serial REST round-trips per symbol, each
            # gated by the 0.06s global throttle → seconds of wall-clock).
            leg_reqs: list[tuple[float, str, dict, str]] = []
            for k in window:
                for opt_t, leg_name in (("CE", "ce"), ("PE", "pe")):
                    row = by_strike[k].get(opt_t)
                    tok = row.get("Token") if row else None
                    if row and tok:
                        leg_reqs.append((k, leg_name, row, tok))
            quotes = self._quotes_batch([("NFO", tok) for *_, tok in leg_reqs])

            def _f(v):
                try:
                    x = float(v)
                    return x if x > 0 else None
                except (ValueError, TypeError):
                    return None

            rows_by_strike: dict[float, StrikeRow] = {}
            for k, leg_name, row, tok in leg_reqs:
                q = quotes.get(tok)
                if not q:
                    continue
                try:
                    leg = OptionLeg(
                        tsym=row.get("TradingSymbol", ""),
                        token=row.get("Token", ""),
                        ltp=float(q["lp"]) if q.get("lp") else None,
                        oi=float(q["oi"]) if q.get("oi") else 0.0,
                        volume=float(q.get("v") or 0),
                        bid=_f(q.get("bp1")), ask=_f(q.get("sp1")))
                except (ValueError, TypeError):
                    continue
                sr = rows_by_strike.get(k)
                if sr is None:
                    sr = StrikeRow(strike=k)
                    rows_by_strike[k] = sr
                setattr(sr, leg_name, leg)
            for k in window:                 # keep strikes in ascending order
                sr = rows_by_strike.get(k)
                if sr is not None and (sr.ce or sr.pe):
                    snap.strikes.append(sr)
            return snap if snap.strikes else None
        try:
            return self._cached(f"chain:{symbol}:{span}", CHAIN_TTL, fetch)
        except Exception as e:
            log.debug("chain %s failed: %s", symbol, e)
            return None
