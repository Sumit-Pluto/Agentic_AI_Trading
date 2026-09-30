"""Intraday engine configuration — typed defaults, JSON-persisted, UI-editable.

One place for every knob. `load()` merges state/intraday_config.json over the
defaults (missing keys fall back), `save()` writes it back. The UI's Settings
tab reads/writes this. Env var INTRADAY_CONFIG overrides the path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
CONFIG_FP = Path(os.environ.get(
    "INTRADAY_CONFIG", _ROOT / "state" / "intraday_config.json"))

DEFAULTS: dict = {
    # ── universe / session ────────────────────────────────────────────────
    "universe": ["NIFTY", "BANKNIFTY"],       # indices first; expand to FnO stocks later
    "underlying_exchange": "NSE",
    "options_exchange": "NFO",
    "bar_timeframe": "5m",                     # 1m | 3m | 5m — the agent/scan bar
    "scan_every_seconds": 30,                  # per-scan cadence during the session
    "oi_sweep_every_seconds": 30,              # /api/_probe/oisweep cadence
    "session_open": "09:15",
    "session_close": "15:30",
    "no_new_entries_after": "15:00",           # stop opening trades late in the day
    "square_off_time": "15:15",                # I0 mandatory flatten (15:10 on expiry)
    "square_off_time_expiry": "15:10",
    "warmup_lookback_minutes": 1500,           # seed bars from /api/candles at start

    # ── money & risk / capital allocation (PDF §2.1; mirrors swing swager) ──
    "total_budget": 100000.0,                  # ₹ this AI system may deploy of the account (self-cap)
    "soft_cap_pct": 80.0,                      # yellow warning at this budget utilisation
    "hard_cap_pct": 90.0,                      # block NEW entries at/above this utilisation
    "global_sl_pct": 5.0,                      # session MTM stop as % of budget -> flatten + halt
    "risk_per_trade_pct": 1.0,                 # % of budget risked per trade
    "max_daily_loss_rupees": 6000.0,           # absolute daily-loss kill (in addition to global_sl_pct)
    "max_positions": 4,
    "max_lots_per_symbol": 10,
    "max_premium_pct_of_equity": 25.0,         # cap total premium outlay per trade
    "max_margin_utilisation_pct": 60.0,        # block entries above this account-margin use
    "margin_safety_factor": 1.10,              # require free margin >= required * this (shared account)
    "atr_stop_mult": 1.5,                      # stop distance floor = mult * ATR(5m)
    "max_prem_loss_pct": 40.0,                 # secondary premium stop for long options
    "equity_rupees": 100000.0,                 # legacy alias; total_budget takes precedence

    # ── exits (I0–I8) ─────────────────────────────────────────────────────
    "target_r_1": 1.0, "partial_pct_1": 50.0,  # T1: book 50% at +1R, move to breakeven
    "target_r_2": 2.0, "partial_pct_2": 30.0,  # T2
    "trail_atr_mult": 2.5,                      # chandelier trail after +1R (ADX-gated wider)
    "stall_bars": 12,                           # I5: exit if age>=12 bars and R<+0.5
    "max_spread_pct": 8.0,                      # I2 liquidity guard on held leg

    # ── decision thresholds ───────────────────────────────────────────────
    "score_threshold": 60.0,                    # min composite to fire
    "score_margin": 10.0,                       # winning side must beat the other by this
    "atm_window_strikes": 10,                    # +/- strikes to fetch around ATM

    # ── mode / safety ─────────────────────────────────────────────────────
    "mode": "paper",                            # paper | live (live gated by typed confirm)
    "paused": False,
    "slippage_pct": 0.10,                        # paper-fill slippage assumption

    # ── server / demo ─────────────────────────────────────────────────────
    "engine_mode": "sim",                        # sim (simulated market) | live (Gateway)
    "demo_step_seconds": 2.0,                    # loop/broadcast cadence in the server

    # ── trained-model probability filter (state/model_full.pkl) ────────────
    "model_filter_enabled": False,               # gate signals on the trained model's P(win)
    "model_filter_min_prob": 0.50,               # take/upsize only above this win-probability
    "model_path": "",                            # blank = state/model_full.pkl (or $INTRADAY_MODEL)

    # ── LLM (PDF §6) — OFF; interface only, template fallback ──────────────
    "llm_enabled": False,
    "llm_base_url": "",
    "llm_model": "",
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(fp: Path | str | None = None) -> dict:
    """Defaults with the on-disk overrides merged in (missing keys fall back)."""
    path = Path(fp or CONFIG_FP)
    if not path.exists():
        return dict(DEFAULTS)
    try:
        user = json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    return _deep_merge(DEFAULTS, user if isinstance(user, dict) else {})


def save(cfg: dict, fp: Path | str | None = None) -> None:
    path = Path(fp or CONFIG_FP)
    path.parent.mkdir(parents=True, exist_ok=True)
    # persist only the diff vs defaults, so new defaults propagate automatically
    diff = {k: v for k, v in (cfg or {}).items()
            if k not in DEFAULTS or DEFAULTS[k] != v}
    path.write_text(json.dumps(diff, indent=2, sort_keys=True))
