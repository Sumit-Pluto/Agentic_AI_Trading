"""Money & Risk Management Agent (PDF §2.1) — intraday.

Ported in spirit from swing_hyena/risk/governor.py, with the overnight-gap term
replaced by an intraday ATR/premium model (plan Q6):

  • size in whole LOTS, on premium-at-risk
  • stop distance floor = k * ATR(5m); risk/share via BS-reprice of the leg at
    the underlying stop, capped at the premium paid (a long option can't lose more)
  • daily-loss HARD KILL — flatten + halt for the day (the top intraday addition)
  • per-symbol lot cap, premium-outlay cap, portfolio-heat cap, margin gate
  • drawdown + regime scalars scale the risk budget, never the scores
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..options.mathx import bs_price


@dataclass
class SizeResult:
    lots: int
    qty: int
    risk_per_share: float
    stop_prem: float
    budget: float
    caps: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.qty > 0


class Governor:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    # ---------- capital ----------
    def budget(self) -> float:
        return float(self.cfg.get("total_budget")
                     or self.cfg.get("equity_rupees", 100000.0))

    def deployed_premium(self, open_positions) -> float:
        """₹ currently committed as option premium across open longs (the budget
        utilisation numerator; premium paid is the capital at work)."""
        return sum(max(getattr(p, "entry_px", 0.0), 0.0) * getattr(p, "qty", 0)
                   for p in (open_positions or []) if getattr(p, "is_long", True))

    def utilisation_pct(self, open_positions) -> float:
        b = self.budget()
        return (self.deployed_premium(open_positions) / b * 100.0) if b > 0 else 0.0

    def can_open_new(self, open_positions, extra_premium: float = 0.0) -> tuple[bool, str]:
        """Block a NEW entry once projected budget utilisation hits the hard cap.
        Averaging into an existing position may use the reserve; new entries may not."""
        b = self.budget()
        if b <= 0:
            return False, "no budget configured"
        hard = float(self.cfg.get("hard_cap_pct", 90.0))
        projected = (self.deployed_premium(open_positions) + max(extra_premium, 0.0)) / b * 100.0
        if projected >= hard:
            return False, f"budget hard cap {hard:.0f}% (projected {projected:.0f}%)"
        return True, "ok"

    def cap_state(self, open_positions) -> str:
        u = self.utilisation_pct(open_positions)
        if u >= float(self.cfg.get("hard_cap_pct", 90.0)):
            return "EXHAUSTED"
        if u >= float(self.cfg.get("soft_cap_pct", 80.0)):
            return "SOFT_CAP"
        return "OK"

    # ---------- hard limits ----------
    def daily_loss_breached(self, realized_pnl_today: float,
                            unrealized_pnl: float = 0.0) -> bool:
        """True → flatten everything and stop trading for the day (§2.1).
        Trips on the absolute ₹ daily-loss limit OR the global MTM stop
        (global_sl_pct of budget), whichever is hit first."""
        session = realized_pnl_today + unrealized_pnl
        abs_limit = float(self.cfg.get("max_daily_loss_rupees", 6000.0))
        if session <= -abs(abs_limit):
            return True
        gsl = float(self.cfg.get("global_sl_pct", 0.0))
        if gsl > 0 and session <= -abs(gsl / 100.0 * self.budget()):
            return True
        return False

    def drawdown_scalar(self, equity_curve: list[float]) -> float:
        """Throttle risk as the session drawdown from peak deepens."""
        pts = [e for e in (equity_curve or []) if e and math.isfinite(e)]
        if len(pts) < 2:
            return 1.0
        peak = max(pts)
        if peak <= 0:
            return 1.0
        dd = (peak - pts[-1]) / peak
        if dd < 0.05:
            return 1.0
        if dd < 0.10:
            return 0.75
        return 0.5

    def margin_ok(self, funds: dict, est_margin: float) -> bool:
        """Block entries once margin utilisation would exceed the cap."""
        if not funds:
            return True                          # unknown funds → don't block (paper)
        try:
            cash = float(funds.get("cash", funds.get("payin", 0)) or 0)
            used = float(funds.get("margin_used", 0) or 0)
        except (TypeError, ValueError):
            return True
        equity = cash + used
        if equity <= 0:
            return True
        cap = float(self.cfg.get("max_margin_utilisation_pct", 60.0)) / 100.0
        return (used + max(est_margin, 0.0)) <= cap * equity

    # ---------- sizing ----------
    def size(self, *, instrument: dict, chain, sl_pts: float, equity: float,
             regime_scalar: float = 1.0, open_positions: list | None = None,
             equity_curve: list[float] | None = None) -> SizeResult:
        """Lots to trade for one long-premium entry.

        instrument: {strike, right, is_call?, lot_size, entry_prem, iv?}
        chain:      the option Chain (spot, t_years, r); leg IV read from it
        sl_pts:     underlying stop distance in points (loop passes max(struct, k*ATR))
        """
        cfg = self.cfg
        lot = int(instrument.get("lot_size") or (chain.lot_size if chain else 0) or 0)
        entry = float(instrument.get("entry_prem") or 0.0)
        if lot <= 0 or entry <= 0:
            return SizeResult(0, 0, 0.0, 0.0, 0.0, reason="no lot size / entry premium")

        is_call = str(instrument.get("right", "")).upper().startswith("C") \
            if "is_call" not in instrument else bool(instrument["is_call"])
        strike = float(instrument["strike"])
        spot = chain.spot
        t, r = chain.t_years, chain.r
        iv = instrument.get("iv") or (chain.get(strike, is_call).iv if chain.get(strike, is_call) else None)

        # risk/share: reprice the leg at the UNDERLYING stop; a long option can't
        # lose more than its premium, and won't fall below the premium floor
        # before the stop fires.
        adverse_spot = spot - sl_pts if is_call else spot + sl_pts
        if iv and iv > 0:
            stop_prem = bs_price(is_call, max(adverse_spot, 0.01), strike, t, iv, r)
        else:
            stop_prem = entry * 0.5              # no IV: assume half-premium stop
        floor_prem = entry * (1.0 - float(cfg.get("max_prem_loss_pct", 40.0)) / 100.0)
        stop_prem = max(stop_prem, floor_prem)
        risk_per_share = max(entry - stop_prem, max(0.05, entry * 0.02))

        dd = self.drawdown_scalar(equity_curve or [])
        budget = (equity * float(cfg.get("risk_per_trade_pct", 1.0)) / 100.0
                  * dd * max(0.0, min(1.0, regime_scalar)))
        lots = int(budget // (risk_per_share * lot)) if risk_per_share > 0 else 0

        # premium-outlay cap
        prem_cap_r = float(cfg.get("max_premium_pct_of_equity", 25.0)) / 100.0 * equity
        lots_prem = int(prem_cap_r // (entry * lot)) if entry * lot > 0 else 0
        # portfolio-heat cap (premium-at-risk across open positions)
        open_positions = open_positions or []
        open_risk = sum(getattr(p, "risk_per_share", 0.0) * getattr(p, "qty", 0)
                        for p in open_positions)
        heat_cap_r = float(cfg.get("max_portfolio_heat_pct",
                                   float(cfg.get("risk_per_trade_pct", 1.0)) * 3.0)) / 100.0 * equity
        heat_room = max(heat_cap_r - open_risk, 0.0)
        lots_heat = int(heat_room // (risk_per_share * lot)) if risk_per_share * lot > 0 else 0
        # per-symbol lot cap
        lots_symcap = int(cfg.get("max_lots_per_symbol", 10))

        final = max(0, min(lots, lots_prem, lots_heat, lots_symcap))
        caps = {"risk_budget_lots": lots, "premium_cap_lots": lots_prem,
                "heat_cap_lots": lots_heat, "symbol_cap_lots": lots_symcap,
                "dd_scalar": dd, "regime_scalar": regime_scalar,
                "open_risk": round(open_risk, 0)}
        binder = min((("risk", lots), ("premium", lots_prem), ("heat", lots_heat),
                      ("symbol", lots_symcap)), key=lambda kv: kv[1])[0]
        return SizeResult(lots=final, qty=final * lot, risk_per_share=risk_per_share,
                          stop_prem=stop_prem, budget=budget, caps=caps,
                          reason=(f"{final} lots (binding: {binder})" if final
                                  else "sized to zero (a cap or budget blocked it)"))
