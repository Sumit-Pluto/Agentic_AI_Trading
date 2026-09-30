"""GatewayChainsContext — the coexistence-safe live data source.

Gets option chains by READING OptionSmith's SQLite store (`chains.db`, table
`chain_snapshot`) — never by driving its own OI sweep, because the Gateway has no
arbitration on the depth path and a second sweeper would wedge the feed for every
system on the box. Underlying 5-min bars come from `/api/candles` (plain REST
TPSeries — NOT the contended socket). VIX + previous-day levels also via REST.

This is the production context: swap it in for SimContext and the same agents/
risk/rules/exits trade the live shared feed with zero added broker load.
See [[server-integration-plan]].
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path

import pandas as pd

from ..gateway_client import option_tradingsymbol
from ..options import greeks_for  # noqa: F401 (used by callers via context)
from ..options.models import IST, MARKET_CLOSE, MARKET_OPEN, Chain, OptionQuote, ist_now
from .context import IntradayContext

# INDIA VIX default identifiers (overridable via cfg["vix_exch"]/["vix_token"])
_VIX_EXCH, _VIX_TOKEN = "NSE", "26017"


def _chains_db_path(cfg: dict) -> Path:
    p = cfg.get("chains_db_path") or os.environ.get("OPTIONSMITH_CHAIN_DB")
    if p:
        return Path(p)
    # default: OptionSmith repo layout on the server
    return Path(os.environ.get("OPTIONSMITH_HOME", "/opt/gateway_system/OptionSmith")) / "data" / "chains.db"


class GatewayChainsContext(IntradayContext):
    def __init__(self, client, cfg: dict):
        self.client = client
        self.cfg = cfg
        self._db = str(_chains_db_path(cfg))
        self._cx: sqlite3.Connection | None = None
        self._tf = str(cfg.get("bar_timeframe", "5m"))
        self._tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(self._tf, 5)
        self._chain_cache: dict[str, tuple[float, Chain]] = {}
        self._bar_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._tok: dict[str, tuple[str, str]] = {}
        self._prevday: dict[str, dict] = {}
        self._pcr: dict[str, float] = {}
        self._vix_cache: tuple[float, float | None] = (0.0, None)
        self._spot: dict[str, float] = {}

    # ---- sqlite (read-only cross-process; OptionSmith opens the writer in WAL) ----
    def _conn(self) -> sqlite3.Connection | None:
        if self._cx is not None:
            return self._cx
        if not Path(self._db).exists():
            return None
        c = sqlite3.connect(self._db, timeout=5, check_same_thread=False)
        c.execute("PRAGMA busy_timeout=5000")
        c.row_factory = sqlite3.Row
        self._cx = c
        return c

    # ---- clock / universe ----
    def now(self) -> dt.datetime:
        return ist_now()

    def symbols(self) -> list[str]:
        uni = self.cfg.get("universe")
        if uni:
            return list(uni)
        c = self._conn()
        if c is None:
            return []
        try:
            rows = c.execute(
                "SELECT symbol FROM chain_snapshot WHERE gate_ok=1 ORDER BY symbol").fetchall()
            return [r["symbol"] for r in rows]
        except sqlite3.Error:
            return []

    @property
    def index_symbol(self) -> str:
        return self.cfg.get("index_symbol", "NIFTY")

    def is_square_off(self, now: dt.datetime | None = None) -> bool:
        now = now or self.now()
        try:
            h, m = (int(x) for x in str(self.cfg.get("square_off_time", "15:15")).split(":"))
        except ValueError:
            h, m = 15, 15
        return now.timetz() >= dt.time(h, m, tzinfo=IST)

    # ---- option chain from OptionSmith's store ----
    def chain(self, symbol: str, expiry_index: int = 0) -> Chain | None:
        ttl = float(self.cfg.get("chain_cache_ttl_s", 5))
        hit = self._chain_cache.get(symbol)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        ch = self._read_chain(symbol)
        if ch is not None:
            self._chain_cache[symbol] = (time.time(), ch)
            self._spot[symbol] = ch.spot
        return ch

    def _read_chain(self, symbol: str) -> Chain | None:
        c = self._conn()
        if c is None:
            return None
        try:
            row = c.execute(
                "SELECT symbol,expiry,spot,lot_size,built_at,quote_span_s,source,"
                "gate_ok,legs,stored_at,carry_rate,exchange FROM chain_snapshot "
                "WHERE symbol=?", (symbol.upper(),)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        if self.cfg.get("require_gate_ok", True) and not row["gate_ok"]:
            return None
        max_age = float(self.cfg.get("chains_max_age_s", 120))
        if max_age > 0 and (time.time() - row["stored_at"]) > max_age:
            return None            # stale — OptionSmith's OI board may be down
        try:
            legs = json.loads(row["legs"])
        except (TypeError, ValueError):
            return None
        exch = row["exchange"] or "NFO"
        expiry = dt.date.fromisoformat(row["expiry"])
        quotes: list[OptionQuote] = []
        for l in legs:
            is_call = str(l.get("r", "")).upper().startswith("C")
            strike = float(l.get("k", 0) or 0)
            if strike <= 0:
                continue
            tsym = option_tradingsymbol(symbol, expiry, is_call, strike)
            quotes.append(OptionQuote(
                strike=strike, is_call=is_call, ltp=float(l.get("ltp") or 0),
                bid=float(l.get("b") or 0), ask=float(l.get("a") or 0),
                oi=int(l.get("oi") or 0), prev_oi=int(l.get("poi") or 0),
                volume=int(l.get("v") or 0), iv=(l.get("iv") if l.get("iv") else None),
                prev_close=float(l.get("pc") or 0), bid_qty=int(l.get("bq") or 0),
                ask_qty=int(l.get("sq") or 0), fetched_at=l.get("ft"),
                tsym=tsym, token="", lot_size=int(row["lot_size"] or 0)))
        return Chain(symbol=row["symbol"], spot=float(row["spot"]), expiry=expiry,
                     lot_size=int(row["lot_size"] or 0), quotes=quotes, asof=ist_now(),
                     exchange=exch, source=f"chains.db:{row['source'] or ''}",
                     quote_span_s=row["quote_span_s"] or 0.0,
                     built_at=row["built_at"], carry_rate=row["carry_rate"])

    def spot(self, symbol: str) -> float:
        if symbol in self._spot:
            return self._spot[symbol]
        ch = self.chain(symbol)
        return ch.spot if ch else 0.0

    def positioning(self, symbol: str) -> dict:
        ch = self.chain(symbol)
        if not ch:
            return {}
        from ..agents.base import chain_features
        pcr = chain_features(ch).get("pcr_oi")
        d_pcr = 0.0
        if pcr is not None and pcr == pcr:
            prev = self._pcr.get(symbol)
            d_pcr = (pcr - prev) if prev is not None else 0.0
            self._pcr[symbol] = pcr
        return {"d_pcr": d_pcr}

    # ---- underlying bars via /api/candles (REST, not the contended socket) ----
    def _resolve_token(self, symbol: str) -> tuple[str, str] | None:
        if symbol in self._tok:
            return self._tok[symbol]
        m = (self.cfg.get("underlying_tokens") or {}).get(symbol)
        if m and "|" in str(m):
            exch, token = str(m).split("|", 1)
            self._tok[symbol] = (exch, token)
            return self._tok[symbol]
        # resolve via Gateway search: prefer the index, else the NSE equity
        try:
            q = symbol if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else f"{symbol}-EQ"
            res = self.client.search(q, exchange="NSE") if hasattr(self.client, "search") else []
        except Exception:
            res = []
        for r in (res or []):
            tok = str(r.get("token") or "")
            if tok:
                self._tok[symbol] = (r.get("exch", "NSE"), tok)
                return self._tok[symbol]
        return None

    def bars(self, symbol: str, tf: str = "5m"):
        ttl = float(self.cfg.get("bar_cache_ttl_s", 45))
        hit = self._bar_cache.get(symbol)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        tok = self._resolve_token(symbol)
        if not tok:
            return hit[1] if hit else None
        try:
            res = self.client.candles(tok[0], tok[1], interval=self._tf_min,
                                      lookback_minutes=self._tf_min * 240)
            candles = res.get("candles", []) if isinstance(res, dict) else res
        except Exception:
            return hit[1] if hit else None
        if not candles:
            return hit[1] if hit else None
        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        df["prev_close"] = df["close"].shift(1).fillna(df["open"])
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cv = df["volume"].cumsum().replace(0, float("nan"))
        df["vwap"] = ((tp * df["volume"]).cumsum() / cv).fillna(df["close"])
        self._bar_cache[symbol] = (time.time(), df)
        if len(df):
            self._spot.setdefault(symbol, float(df["close"].iloc[-1]))
        return df

    def vwap(self, symbol: str) -> float:
        b = self.bars(symbol)
        return float(b["vwap"].iloc[-1]) if b is not None and len(b) else 0.0

    def vix(self) -> float | None:
        if time.time() - self._vix_cache[0] < 60:
            return self._vix_cache[1]
        exch = self.cfg.get("vix_exch", _VIX_EXCH)
        token = self.cfg.get("vix_token", _VIX_TOKEN)
        val = None
        try:
            q = self.client.quote(exch, token)
            val = float(q.get("lp") or q.get("ltp") or 0) or None
        except Exception:
            val = None
        self._vix_cache = (time.time(), val)
        return val

    def prev_day(self, symbol: str) -> dict:
        if symbol in self._prevday:
            return self._prevday[symbol]
        tok = self._resolve_token(symbol)
        if not tok:
            return {}
        try:
            res = self.client.candles(tok[0], tok[1], daily=True, days=5,
                                      tradingsymbol=symbol)
            candles = res.get("candles", []) if isinstance(res, dict) else res
        except Exception:
            candles = []
        # last COMPLETED day = second-to-last daily bar (today's is forming)
        if len(candles) >= 2:
            d = candles[-2]
            self._prevday[symbol] = {"pdh": float(d["high"]), "pdl": float(d["low"]),
                                     "pdc": float(d["close"]), "pdo": float(d["open"])}
        return self._prevday.get(symbol, {})
