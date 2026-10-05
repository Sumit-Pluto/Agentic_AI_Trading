"""Trade DB & journal (PDF §4 + §2.5) — SQLite, one file, WAL mode.

Ported from swing_hyena (journal/store.py), adapted for intraday:
  • signals/positions/trades carry the option instrument + intraday timestamps
  • equity marks are intraday (keyed by ts), not one row per day
  • orders keep the latency stamps that satisfy PDF §7 (<5s signal→submit)

Everything the engine does leaves a row here; the UI + reporting read only here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

def _db_path(fp=None) -> Path:
    """Resolve the DB path at call time so a later INTRADAY_DB is honoured."""
    return Path(fp or os.environ.get("INTRADAY_DB")
                or (Path(__file__).resolve().parents[1] / "state" / "intraday.db"))


DB_FP = _db_path()   # back-compat module attribute

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY, ts REAL, date TEXT, symbol TEXT, direction TEXT,
  score_buy REAL, score_sell REAL, families TEXT, agents TEXT,
  vetoes TEXT, n_scored INTEGER, regime TEXT, brain_version TEXT,
  instrument TEXT, acted INTEGER DEFAULT 0, created_ts REAL);
CREATE INDEX IF NOT EXISTS ix_signals_date ON signals(date);

CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, signal_id INTEGER, symbol TEXT, side TEXT, qty INTEGER,
  order_type TEXT, limit_px REAL, status TEXT, broker TEXT, broker_order_id TEXT,
  reason TEXT, strategy TEXT, retries INTEGER DEFAULT 0,
  signal_ts REAL, submitted_ts REAL, acked_ts REAL, filled_ts REAL,
  fill_px REAL, latency_ms REAL, date TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_date ON orders(date);

CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY, symbol TEXT, underlying TEXT, side TEXT, qty INTEGER,
  strike REAL, right TEXT, expiry TEXT, lot_size INTEGER, exch TEXT, token TEXT,
  entry_px REAL, entry_ts TEXT, entry_spot REAL, delta_at_entry REAL,
  stop REAL, risk_per_share REAL, age_bars INTEGER, max_prem REAL,
  breakeven INTEGER, partial INTEGER, strategy TEXT,
  status TEXT DEFAULT 'OPEN', exit_px REAL, exit_ts TEXT, exit_reason TEXT,
  log TEXT DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(status);

CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, position_id INTEGER, symbol TEXT, underlying TEXT,
  side TEXT, strategy TEXT, entry_ts TEXT, exit_ts TEXT, entry_px REAL,
  exit_px REAL, qty INTEGER, pnl REAL, r REAL, hold_bars INTEGER,
  exit_reason TEXT, brain_version TEXT, date TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_date ON trades(date);

CREATE TABLE IF NOT EXISTS equity(         -- intraday marks, keyed by timestamp
  ts REAL PRIMARY KEY, date TEXT, equity REAL, cash REAL, open_risk REAL,
  realized_pnl REAL, unrealized_pnl REAL, n_positions INTEGER,
  regime_on INTEGER, regime_scalar REAL);
CREATE INDEX IF NOT EXISTS ix_equity_date ON equity(date);

CREATE TABLE IF NOT EXISTS agent_stats(
  date TEXT, agent TEXT, family TEXT, scored INTEGER, na INTEGER, veto INTEGER,
  PRIMARY KEY(date, agent));

CREATE TABLE IF NOT EXISTS brains(
  version TEXT PRIMARY KEY, kind TEXT, path TEXT, trained_at TEXT,
  train_window TEXT, metrics TEXT, active INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, name TEXT, started TEXT, finished TEXT,
  status TEXT, detail TEXT);
"""


class Store:
    def __init__(self, fp: Path | str | None = None):
        fp = _db_path(fp)
        Path(fp).parent.mkdir(parents=True, exist_ok=True)
        self.cx = sqlite3.connect(str(fp), check_same_thread=False)
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.row_factory = sqlite3.Row
        self.cx.executescript(SCHEMA)
        self.cx.commit()
        # One connection is shared by the engine thread and the API threads:
        # serialise every op so a UI poll can never collide with a journal
        # write ("Recursive use of cursors" / "database is locked").
        self._lock = threading.RLock()

    # ---------------- generic helpers
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.cx.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self.cx.execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.cx.execute(sql, args)
            self.cx.commit()
            return cur.lastrowid

    # ---------------- signals
    def save_signal(self, s) -> int:
        return self.x(
            "INSERT INTO signals(ts,date,symbol,direction,score_buy,score_sell,"
            "families,agents,vetoes,n_scored,regime,brain_version,instrument,created_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.ts.timestamp(), str(s.date), s.symbol, s.direction, s.score_buy,
             s.score_sell, json.dumps(s.family_scores), json.dumps(s.agent_rows),
             json.dumps(s.vetoes), s.n_scored, json.dumps(s.regime),
             s.brain_version, json.dumps(s.instrument or {}), time.time()))

    def mark_signal_acted(self, signal_id: int):
        self.x("UPDATE signals SET acted=1 WHERE id=?", (signal_id,))

    # ---------------- orders (latency-stamped, PDF §7)
    def save_order(self, intent, status: str, broker: str, date: dt.date,
                   broker_order_id: str = "", latency_ms: float | None = None,
                   retries: int = 0, submitted_ts: float | None = None) -> int:
        return self.x(
            "INSERT INTO orders(signal_id,symbol,side,qty,order_type,limit_px,status,"
            "broker,broker_order_id,reason,strategy,retries,signal_ts,submitted_ts,"
            "latency_ms,date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent.signal_id, intent.symbol, intent.side, intent.qty,
             intent.order_type, intent.limit_px, status, broker, broker_order_id,
             intent.reason, getattr(intent, "strategy", "default"), retries,
             intent.signal_ts, submitted_ts or time.time(), latency_ms, str(date)))

    def fill_order(self, order_id: int, px: float):
        self.x("UPDATE orders SET status='FILLED', fill_px=?, filled_ts=? WHERE id=?",
               (px, time.time(), order_id))

    def set_order_status(self, order_id: int, status: str):
        self.x("UPDATE orders SET status=? WHERE id=?", (status, order_id))

    # ---------------- positions
    def open_position(self, p) -> int:
        return self.x(
            "INSERT INTO positions(symbol,underlying,side,qty,strike,right,expiry,"
            "lot_size,exch,token,entry_px,entry_ts,entry_spot,delta_at_entry,stop,"
            "risk_per_share,age_bars,max_prem,breakeven,partial,strategy) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.symbol, p.underlying, p.side, p.qty, p.strike, p.right,
             (str(p.expiry) if p.expiry else None), p.lot_size, p.exch, p.token,
             p.entry_px, p.entry_ts.isoformat(), p.entry_spot, p.delta_at_entry,
             p.stop, p.risk_per_share, p.age_bars, p.max_prem,
             int(p.breakeven_done), int(p.partial_done), p.strategy))

    def update_position(self, p):
        self.x("UPDATE positions SET stop=?, age_bars=?, max_prem=?, breakeven=?, "
               "partial=?, qty=?, log=? WHERE id=?",
               (p.stop, p.age_bars, p.max_prem, int(p.breakeven_done),
                int(p.partial_done), p.qty, json.dumps(p.log[-50:]), p.position_id))

    def close_position(self, p, brain_version: str = "equal-v0",
                       cost_per_share: float = 0.0) -> int:
        self.x("UPDATE positions SET status='CLOSED', exit_px=?, exit_ts=?, "
               "exit_reason=? WHERE id=?",
               (p.exit_px, (p.exit_ts.isoformat() if p.exit_ts else None),
                p.exit_reason, p.position_id))
        sign = 1.0 if p.is_long else -1.0
        pnl = sign * ((p.exit_px or 0.0) - p.entry_px) * p.qty
        pnl -= max(cost_per_share or 0.0, 0.0) * p.qty   # round-trip costs
        r = (sign * ((p.exit_px or 0.0) - p.entry_px) / p.risk_per_share
             if p.risk_per_share else 0.0)
        return self.x(
            "INSERT INTO trades(position_id,symbol,underlying,side,strategy,entry_ts,"
            "exit_ts,entry_px,exit_px,qty,pnl,r,hold_bars,exit_reason,brain_version,date) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.position_id, p.symbol, p.underlying, p.side, p.strategy,
             p.entry_ts.isoformat(), (p.exit_ts.isoformat() if p.exit_ts else None),
             p.entry_px, p.exit_px, p.qty, pnl, r, p.age_bars, p.exit_reason,
             brain_version, str(p.entry_ts.date())))

    def book_partial(self, p, exit_px: float, qty: int, reason: str,
                     exit_ts, brain_version: str = "equal-v0",
                     cost_per_share: float = 0.0) -> int:
        """Book a PARTIAL exit (I3): record a trade for `qty` shares and reduce
        the open position's qty. The position stays OPEN with the remainder."""
        sign = 1.0 if p.is_long else -1.0
        pnl = sign * (exit_px - p.entry_px) * qty
        pnl -= max(cost_per_share or 0.0, 0.0) * qty     # round-trip costs
        r = (sign * (exit_px - p.entry_px) / p.risk_per_share) if p.risk_per_share else 0.0
        tid = self.x(
            "INSERT INTO trades(position_id,symbol,underlying,side,strategy,entry_ts,"
            "exit_ts,entry_px,exit_px,qty,pnl,r,hold_bars,exit_reason,brain_version,date) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.position_id, p.symbol, p.underlying, p.side, p.strategy,
             p.entry_ts.isoformat(), (exit_ts.isoformat() if exit_ts else None),
             p.entry_px, exit_px, qty, pnl, r, p.age_bars,
             (reason + " (partial)")[:64], brain_version, str(p.entry_ts.date())))
        p.qty = max(0, p.qty - qty)
        self.x("UPDATE positions SET qty=? WHERE id=?", (p.qty, p.position_id))
        return tid

    def open_positions(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM positions WHERE status='OPEN'")

    # ---------------- equity / stats / jobs
    def mark_equity(self, ts: float, date: dt.date, equity: float, cash: float,
                    open_risk: float, realized: float, unrealized: float,
                    n_pos: int, regime_on: bool, scalar: float):
        self.x("INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?,?,?,?,?,?)",
               (ts, str(date), equity, cash, open_risk, realized, unrealized,
                n_pos, int(regime_on), scalar))

    def save_agent_stats(self, date: dt.date, rows: list[dict]):
        for r in rows:
            self.x("INSERT OR REPLACE INTO agent_stats VALUES(?,?,?,?,?,?)",
                   (str(date), r["agent"], r["family"], r.get("scored", 0),
                    r.get("na", 0), r.get("veto", 0)))

    def realized_pnl_today(self, date: dt.date) -> float:
        r = self.one("SELECT COALESCE(SUM(pnl),0) AS s FROM trades WHERE date=?",
                     (str(date),))
        return float(r["s"]) if r else 0.0

    def job(self, name: str) -> int:
        return self.x("INSERT INTO jobs(name,started,status) VALUES(?,?,?)",
                      (name, dt.datetime.now().isoformat(timespec="seconds"), "RUNNING"))

    def job_done(self, job_id: int, status: str = "OK", detail: str = ""):
        self.x("UPDATE jobs SET finished=?, status=?, detail=? WHERE id=?",
               (dt.datetime.now().isoformat(timespec="seconds"), status, detail, job_id))

    def close(self):
        try:
            self.cx.close()
        except Exception:
            pass
