"""strategist/zeroloss.py — Zero-Loss Strategy Stock Search.

Hunts for DEFINED option structures whose payoff floor (minimum payoff at
expiry over ALL underlying prices) is >= 0 while max profit > 0. At fair
prices such structures are arbitrage and should not exist; they appear only
when illiquid strikes are mispriced.

THE HONESTY RULES (hardened after a user-verified ABB false positive —
a "risk-free box" assembled from stale deep-ITM LTPs):
  1. EVERY leg must have a LIVE two-sided quote. No LTP guessing, ever.
     Market closed / empty book → the scan honestly finds nothing.
  2. Prices are executable + slippage: BUY at ask×(1+1%), SELL at bid×(1−1%),
     minus ₹40/leg-lot friction.
  3. Deep-ITM legs (intrinsic > 80% of mid) are excluded — stale-print traps.
  4. Box/collar (synthetic-forward) families are disabled for stock options:
     even a real box is a physically-settled delivery position, not an arb.
  5. No claims inside delivery week (DTE < 7): NSE stock options settle
     physically; hold-to-expiry floors stop being riskless there.
  "ltp_based": true now means WIDE SPREAD (real but ugly quotes) — verify.
At fair two-sided prices this scan MUST find nothing — the self-test
asserts exactly that, plus the no-quote exclusion and the DTE guard.

Tail safety: a floor over ALL S requires the payoff to be bounded below on
BOTH tails. As S→∞ only calls matter (slope = net call coefficient), so
sum(coef | calls) >= 0 is required; as S→0 puts dominate downside, so
sum(coef | puts) >= 0 is enforced too. With both holding, the payoff is
piecewise-linear with non-positive slope before the first strike and
non-negative slope after the last, hence its global minimum sits on the
strike kinks — which is what the vectorised pre-filter probes.
"""

from __future__ import annotations

import itertools
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np

try:
    from quant.mathutils import years_to_expiry
except ModuleNotFoundError:                      # direct-script execution
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from quant.mathutils import years_to_expiry

from strategist.generator import classify
from strategist.payoff import Leg, evaluate_strategy

log = logging.getLogger("zeroloss")

# ── knobs ────────────────────────────────────────────────────────────────
OI_FLOOR = 300                # leg liquidity guard
SPREAD_FRAC = 0.25            # bid/ask "tight" if spread <= 25% of mid
FRICTION_PER_LEG = 40.0       # ₹ per leg-lot round trip
SLIPPAGE_FRAC = 0.01          # extra 1% haircut per leg on top of bid/ask
INTRINSIC_MAX_FRAC = 0.80     # deep-ITM guard: intrinsic > 80% of mid → skip
                              # (stale deep-ITM prints are the classic fake-box
                              # trap — the ABB 6600/7400 case)
MIN_DTE_DAYS = 7.0            # no zero-loss claims inside delivery week:
                              # NSE stock options are PHYSICALLY SETTLED —
                              # ITM legs at expiry become delivery obligations
                              # with escalating margins from E-4
MAX_PER_TYPE = 24             # contracts per option type (nearest ATM kept)
MAX_EXACT = 60                # pre-filter survivors sent to exact evaluation
MAX_RESULTS = 5               # structures kept per chain
MAX_STOCKS = 50               # stocks kept by the background sweep
UPSIDE_CAP = 25000.0          # ₹ stand-in for UNLIMITED upside in the score
CHAIN_SPAN = 10               # strikes each side of ATM for the sweep

# Same-type (calls-only / puts-only) coefficient families, lowest strike
# first. Sets whose coefficients sum negative are skipped at runtime by the
# tail-safety rule (e.g. (1,-2,1,-1) leaves a naked short tail).
_SAME_TYPE: list[tuple[tuple[int, ...], str]] = [
    ((1, -1), "vertical"), ((-1, 1), "vertical"),
    ((1, -2, 1), "butterfly"), ((-1, 2, -1), "short butterfly"),
    ((-1, 2, -1, 1), "broken-wing"), ((1, -2, 1, -1), "broken-wing"),
    ((1, -1, -1, 1), "condor"), ((-1, 1, 1, -1), "short condor"),
]
# Mixed 2-call + 2-put family (boxes / collars) — REMOVED for stock options:
# a "cheap box" on an NSE stock chain is virtually always two stale deep-ITM
# LTPs, and even a real one is a physically-settled delivery position, not a
# retail arb (the user-verified ABB 6600/7400 false positive). Empty list
# kept so the sweep code path stays intact if index chains are added later.
_MIXED_2C2P: list[tuple[tuple[int, ...], str]] = []


@dataclass
class ZContract:
    """One tradeable option with CONSERVATIVE executable prices."""
    is_call: bool
    strike: float
    oi: float
    tsym: str
    ltp: float | None
    bid: float | None
    ask: float | None
    buy_px: float       # what buying really costs   (ask, or ltp*1.02)
    sell_px: float      # what selling really earns  (bid, or ltp*0.98)
    ltp_based: bool     # True → prices are LTP guesses, UI must show VERIFY


# ── conservative universe ────────────────────────────────────────────────
def _price_contract(leg, strike: float, is_call: bool,
                    spot: float = 0.0) -> ZContract | None:
    """Apply liquidity + honesty rules to one chain leg; None = unusable."""
    if leg is None:
        return None
    try:
        oi = float(leg.oi) if leg.oi is not None else 0.0
        ltp = float(leg.ltp) if leg.ltp else None
        bid = float(leg.bid) if getattr(leg, "bid", None) else None
        ask = float(leg.ask) if getattr(leg, "ask", None) else None
    except (TypeError, ValueError):
        return None
    if oi < OI_FLOOR:
        return None
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask <= 0:
        ask = None

    # HARD RULE (post-ABB): a zero-loss claim requires a LIVE two-sided
    # quote on EVERY leg. No LTP guessing, ever — stale deep-ITM prints are
    # exactly how fake "risk-free boxes" are born. A wide-but-real spread is
    # allowed (the floor is computed at those bad prices anyway) but flagged.
    if bid is None or ask is None or ask < bid:
        return None
    wide = (ask - bid) > SPREAD_FRAC * (0.5 * (ask + bid))

    # deep-ITM guard: intrinsic-dominated premiums are stale-print magnets
    if spot and spot > 0:
        intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
        mid = 0.5 * (ask + bid)
        if intrinsic > 0 and mid > 0 and intrinsic / mid > INTRINSIC_MAX_FRAC:
            return None

    # slippage haircut on top of touching the book
    buy_px = ask * (1.0 + SLIPPAGE_FRAC)
    sell_px = bid * (1.0 - SLIPPAGE_FRAC)
    return ZContract(is_call=is_call, strike=float(strike), oi=oi,
                     tsym=getattr(leg, "tsym", "") or "", ltp=ltp,
                     bid=bid, ask=ask, buy_px=buy_px, sell_px=sell_px,
                     ltp_based=wide)   # flag now means: wide spread — verify


def _universe(chain) -> list[ZContract]:
    out: list[ZContract] = []
    spot = float(getattr(chain, "spot", 0.0) or 0.0)
    for row in chain.strikes:
        for leg, is_call in ((row.ce, True), (row.pe, False)):
            c = _price_contract(leg, row.strike, is_call, spot=spot)
            if c is not None:
                out.append(c)

    def cap(side: list[ZContract]) -> list[ZContract]:
        side.sort(key=lambda c: (abs(c.strike - spot), c.strike))
        side = side[:MAX_PER_TYPE]
        side.sort(key=lambda c: c.strike)
        return side

    calls = cap([c for c in out if c.is_call])
    puts = cap([c for c in out if not c.is_call])
    return calls + puts


# ── vectorised floor pre-filter (exact for piecewise-linear payoffs) ─────
def _combo_floors(INTR: np.ndarray, buy: np.ndarray, sell: np.ndarray,
                  idx: np.ndarray, coef: tuple[int, ...]) -> np.ndarray:
    """Per-share payoff floor of every combo row at CONSERVATIVE prices.

    payoff(S) = Σ_j c_j·intrinsic_j(S) − Σ_j c_j·px_j
    where px_j = ask (buy_px) when c_j > 0, bid (sell_px) when c_j < 0."""
    P = np.zeros((idx.shape[0], INTR.shape[1]))
    debit = np.zeros(idx.shape[0])
    for j, c in enumerate(coef):
        rows = idx[:, j]
        P += float(c) * INTR[rows]
        debit += float(c) * (buy[rows] if c > 0 else sell[rows])
    P -= debit[:, None]
    return P.min(axis=1)


def _exact_floor_ps(legs: list[Leg], far: float) -> float:
    """Exact per-share payoff floor over ALL S for a tail-safe leg set.

    Piecewise-linear payoff + tail safety (net calls >= 0, net puts >= 0)
    → the global minimum sits on a strike kink; 0.01 and a far point are
    probed as belt-and-braces. NOTE: evaluate_strategy.max_loss clamps at 0
    (abs(min(0, ...))) so it cannot express a POSITIVE floor — a credit fly
    whose worst case still keeps the credit needs this exact minimum."""
    pts = sorted({l.strike for l in legs}) + [0.01, far]
    return min(sum(l.payoff_at(s) for l in legs) for s in pts)


def _make_legs(universe: list[ZContract], idx_row,
               coef: tuple[int, ...]) -> tuple[list[Leg], bool, float]:
    """Legs at conservative premiums (|coef|=2 → same Leg twice, Leg has no
    qty field). Returns (legs, any_ltp_based, min_oi)."""
    legs: list[Leg] = []
    ltp_based, min_oi = False, float("inf")
    for j, c in enumerate(coef):
        con = universe[int(idx_row[j])]
        side = 1 if c > 0 else -1
        premium = con.buy_px if c > 0 else con.sell_px
        ltp_based = ltp_based or con.ltp_based
        min_oi = min(min_oi, con.oi)
        for _ in range(abs(int(c))):
            legs.append(Leg(side=side, is_call=con.is_call, strike=con.strike,
                            premium=premium, tsym=con.tsym, oi=con.oi))
    return legs, ltp_based, min_oi


# ── per-chain scan ───────────────────────────────────────────────────────
def scan_chain(chain, friction_per_leg: float = FRICTION_PER_LEG,
               max_loss_tolerance: float = 0.0) -> list[dict]:
    """Return up to MAX_RESULTS structures on this chain whose worst-case
    expiry P&L at executable prices, net of friction, is >= -max_loss_tolerance
    (₹ per lot) with a real profit left to win. Empty list = honest nothing."""
    if chain is None or not getattr(chain, "strikes", None):
        return []
    try:
        spot = float(chain.spot)
        lot = max(1, int(getattr(chain, "lot", 0) or 1))
    except (TypeError, ValueError):
        return []
    if spot <= 0:
        return []
    # delivery-week guard: NSE stock options are physically settled; holding
    # ITM legs into expiry week means delivery margins + assignment risk, so
    # a hold-to-expiry "floor" is no longer riskless there
    try:
        expiry = float(getattr(chain, "expiry_epoch", 0.0) or 0.0)
        if expiry > 0 and (expiry - time.time()) < MIN_DTE_DAYS * 86400:
            return []
    except (TypeError, ValueError):
        pass
    universe = _universe(chain)
    if len(universe) < 2:
        return []

    strikes = np.array([c.strike for c in universe])
    buy = np.array([c.buy_px for c in universe])
    sell = np.array([c.sell_px for c in universe])
    ci = [i for i, c in enumerate(universe) if c.is_call]
    pi = [i for i, c in enumerate(universe) if not c.is_call]

    # probe grid: every strike kink + near-zero + far tail (tail-safe sets
    # make the kink minimum the GLOBAL minimum; extremes are belt-and-braces)
    probe = np.unique(np.concatenate(
        [strikes, [0.01, 3.0 * float(strikes.max())]]))
    INTR = np.where(np.array([c.is_call for c in universe])[:, None],
                    np.maximum(probe[None, :] - strikes[:, None], 0.0),
                    np.maximum(strikes[:, None] - probe[None, :], 0.0))

    # enumerate families → (floor_ps, idx_row, coef, family) survivors
    candidates: list[tuple[float, tuple, tuple[int, ...], str]] = []

    def sweep(idx: np.ndarray, coef: tuple[int, ...], family: str):
        if idx.size == 0:
            return
        n_lots = sum(abs(c) for c in coef)
        friction = friction_per_leg * n_lots
        floors = _combo_floors(INTR, buy, sell, idx, coef)
        ok = floors * lot - friction >= -max_loss_tolerance - 1e-6
        for i in np.nonzero(ok)[0]:
            candidates.append((float(floors[i]), tuple(idx[i]), coef, family))

    for side in (ci, pi):
        for r in (2, 3, 4):
            if len(side) < r:
                continue
            idx = np.array(list(itertools.combinations(side, r)), dtype=int)
            for coef, family in _SAME_TYPE:
                if len(coef) != r:
                    continue
                if sum(coef) < 0:          # tail safety: net type coef >= 0
                    continue
                sweep(idx, coef, family)
    if len(ci) >= 2 and len(pi) >= 2:
        c2 = np.array(list(itertools.combinations(ci, 2)), dtype=int)
        p2 = np.array(list(itertools.combinations(pi, 2)), dtype=int)
        mixed = np.hstack([np.repeat(c2, len(p2), axis=0),
                           np.tile(p2, (len(c2), 1))])
        for coef, family in _MIXED_2C2P:
            if sum(coef[:2]) < 0 or sum(coef[2:]) < 0:     # tail safety
                continue
            sweep(mixed, coef, family)
    if not candidates:
        return []

    # exact confirmation of the best pre-filter survivors
    candidates.sort(key=lambda x: -x[0])
    candidates = candidates[:MAX_EXACT]
    t_years = max(1e-4, years_to_expiry(
        float(getattr(chain, "expiry_epoch", 0.0) or 0.0), time.time()))

    results: list[tuple[float, float, dict]] = []
    for _floor_ps, idx_row, coef, family in candidates:
        legs, ltp_based, min_oi = _make_legs(universe, idx_row, coef)
        if not legs:
            continue
        n_lots = sum(abs(c) for c in coef)
        friction = friction_per_leg * n_lots
        res = evaluate_strategy(family, legs, lot, spot,
                                atm_iv=None, t_years=t_years)
        if math.isinf(res.max_loss):       # never true for tail-safe sets
            continue
        floor_ps = _exact_floor_ps(legs, 3.0 * float(strikes.max()))
        # consistency: evaluate_strategy's max_loss must agree with the
        # clamped floor (max_loss == -min(0, floor)); if not, distrust combo
        if abs(min(0.0, floor_ps) + res.max_loss) > 0.01:
            continue
        net_floor = floor_ps * lot - friction
        if net_floor < -max_loss_tolerance - 1e-6:
            continue
        # something to win: real upside beyond friction
        if math.isinf(res.max_profit):
            upside = UPSIDE_CAP
        else:
            upside = res.max_profit * lot - friction
            if res.max_profit * lot <= friction:
                continue
        label = classify(legs)
        if label == "custom structure":
            label = family
        d = res.to_dict()
        d.update({
            "label": label,
            "family": family,
            "floor_per_lot": round(net_floor, 0),
            "profit_zone": [round(b, 2) for b in res.breakevens],
            "ltp_based": bool(ltp_based),
            "legs_oi_min": round(min_oi, 0),
            "n_lots": n_lots,
            "friction_rupees": round(friction, 0),
            "score": round(net_floor + 0.10 * max(0.0, upside), 2),
        })
        results.append((net_floor, upside, d))

    results.sort(key=lambda x: (-x[0], -x[1]))
    return [d for _f, _u, d in results[:MAX_RESULTS]]


# ── background sweep across the F&O universe ─────────────────────────────
class ZeroLossScan:
    """Day-simulator-style background sweep: .start() spawns a daemon thread
    that walks hub.fo_universe(), scans each chain and keeps the top
    MAX_STOCKS stocks (sorted by best floor). Poll .status / .results."""

    def __init__(self, hub):
        self.hub = hub
        self.status: dict = {"running": False, "done": 0, "total": 0,
                             "found": 0, "error": None, "started_at": None}
        self.results: list[dict] = []
        self._thread: threading.Thread | None = None

    def start(self, limit: int = 0) -> bool:
        """Kick off the sweep in the background; False if already running."""
        if self.status.get("running"):
            return False
        self.status = {"running": True, "done": 0, "total": 0, "found": 0,
                       "error": None,
                       "started_at": datetime.now().isoformat(
                           timespec="seconds")}
        self._thread = threading.Thread(target=self._run,
                                        kwargs={"limit": int(limit or 0)},
                                        name="zeroloss-scan", daemon=True)
        self._thread.start()
        return True

    def _run(self, limit: int = 0):
        self.results = []
        kept: list[dict] = []
        n_found = 0
        try:
            symbols = list(self.hub.fo_universe() or [])
            if limit:
                symbols = symbols[:limit]
            self.status["total"] = len(symbols)
            for symbol in symbols:
                try:
                    chain = self.hub.chain_snapshot(symbol, span=CHAIN_SPAN)
                    if chain is not None:
                        hits = scan_chain(chain)
                        if hits:
                            n_found += 1
                            kept.append({
                                "symbol": symbol,
                                "spot": float(chain.spot),
                                "lot": int(getattr(chain, "lot", 0) or 0),
                                "best_floor": hits[0]["floor_per_lot"],
                                "structures": hits,
                            })
                            kept.sort(key=lambda e: -e["best_floor"])
                            del kept[MAX_STOCKS:]
                            self.results = list(kept)
                            self.status["found"] = n_found
                except Exception as e:      # per-symbol: degrade, keep going
                    log.debug("zeroloss %s failed: %s", symbol, e)
                finally:
                    self.status["done"] += 1
                time.sleep(0)               # datahub already throttles quotes
        except Exception as e:
            self.status["error"] = str(e)
            log.warning("zeroloss sweep aborted: %s", e)
        finally:
            self.status["running"] = False
            log.info("zeroloss sweep done: %d/%d symbols, %d with structures",
                     self.status["done"], self.status.get("total", 0), n_found)


# ── offline self-test (no network) ───────────────────────────────────────
if __name__ == "__main__":
    from quant.context import ChainSnapshot, OptionLeg, StrikeRow
    from quant.mathutils import bs_price

    TICK = 0.05

    def _dn(x: float) -> float | None:
        v = math.floor(x / TICK) * TICK
        return round(v, 2) if v >= TICK else None

    def _up(x: float) -> float:
        return round(max(TICK, math.ceil(x / TICK) * TICK), 2)

    def _near(x: float) -> float:
        return round(max(TICK, round(x / TICK) * TICK), 2)

    def synth_chain(spot=1000.0, n_side=8, step=20.0, lot=250,
                    dte_days=12.0, iv=0.25):
        """Flat-IV Black-Scholes chain = arbitrage-free by construction.
        Ticks rounded AGAINST the scanner (bid down, ask up) so the fair
        chain cannot manufacture a fake edge from rounding."""
        now = time.time()
        t = dte_days / 365.0
        snap = ChainSnapshot(symbol="TESTSTK", lot=lot, spot=spot,
                             expiry_epoch=now + dte_days * 86400)
        for i in range(2 * n_side + 1):
            k = spot + step * (i - n_side)
            row = StrikeRow(strike=k)
            for is_call, name in ((True, "ce"), (False, "pe")):
                fair = bs_price(is_call, spot, k, t, iv)
                leg = OptionLeg(
                    tsym=f"TESTSTK{'C' if is_call else 'P'}{int(k)}",
                    token=f"{int(k)}{'C' if is_call else 'P'}",
                    ltp=_near(fair), oi=1200.0, volume=500.0,
                    bid=_dn(fair * 0.98), ask=_up(fair * 1.02))
                setattr(row, name, leg)
            snap.strikes.append(row)
        return snap

    def leg_at(snap, strike, is_call):
        for r in snap.strikes:
            if abs(r.strike - strike) < 1e-9:
                return r.ce if is_call else r.pe
        raise AssertionError(f"no strike {strike}")

    # 1) FAIR chain: two-sided executable prices, no mispricing → the scan
    #    MUST find NOTHING (floors < 0 everywhere at bid/ask). REQUIRED null.
    fair = synth_chain()
    assert scan_chain(fair) == [], \
        "fair chain must yield ZERO zero-loss structures"

    # 2) PLANTED mispricing: 1040 CE offered absurdly cheap → the credit
    #    call fly 1000/1020/1040 (and put-call-parity boxes) get a true
    #    zero floor at executable prices.
    planted = synth_chain()
    wing = leg_at(planted, 1040.0, True)
    b1020 = leg_at(planted, 1020.0, True).bid
    a1000 = leg_at(planted, 1000.0, True).ask
    cheap = max(TICK, math.floor(
        max(TICK, 2 * b1020 - a1000 - 3.0) / TICK) * TICK)
    wing.bid, wing.ask, wing.ltp = cheap, cheap, cheap     # tight quote

    t0 = time.time()
    hits = scan_chain(planted)
    dt = time.time() - t0
    assert dt < 5.0, f"scan too slow: {dt:.2f}s"
    assert 1 <= len(hits) <= MAX_RESULTS, f"planted chain: {len(hits)} hits"
    for h in hits:
        for key in ("label", "family", "floor_per_lot", "max_profit_per_lot",
                    "profit_zone", "ltp_based", "legs_oi_min", "net_flow",
                    "score", "legs", "friction_rupees"):
            assert key in h, key
        assert h["floor_per_lot"] >= 0, h["floor_per_lot"]
        mp = h["max_profit_per_lot"]
        assert mp == "UNLIMITED" or mp > h["friction_rupees"]
    floors = [h["floor_per_lot"] for h in hits]
    assert floors == sorted(floors, reverse=True), "not sorted by floor"

    # conservative-pricing honesty: some hit BUYS the planted wing and its
    # premium must equal ASK plus the slippage haircut (not LTP, not mid)
    exp_buy = cheap * (1.0 + SLIPPAGE_FRAC)
    wing_hits = [h for h in hits for l in h["legs"]
                 if l["side"] == "BUY" and l["type"] == "CE"
                 and abs(l["strike"] - 1040.0) < 1e-9
                 and abs(l["premium"] - exp_buy) < 1e-9]
    assert wing_hits, "no hit bought the planted 1040 CE at ask+slippage"
    assert wing_hits[0]["ltp_based"] is False    # tight-quoted structure
    # SELL legs of that hit must be priced at BID minus the slippage haircut
    for l in wing_hits[0]["legs"]:
        if l["side"] == "SELL":
            src = leg_at(planted, l["strike"], l["type"] == "CE")
            assert abs(l["premium"] - src.bid * (1.0 - SLIPPAGE_FRAC)) < 1e-9, \
                "SELL leg not at bid-slippage"

    # 3) NO-QUOTE EXCLUSION (post-ABB rule): same plant but the wing has NO
    #    bid/ask, only LTP → the leg is unusable, so the whole chain (fair
    #    everywhere else) must yield ZERO hits. LTP guessing is dead.
    planted2 = synth_chain()
    wing2 = leg_at(planted2, 1040.0, True)
    wing2.bid, wing2.ask, wing2.ltp = None, None, cheap
    hits2 = scan_chain(planted2)
    assert hits2 == [], "LTP-only leg must be excluded, not guessed"

    # 3b) expiry-week guard: the same planted arb inside delivery week must
    #     be refused (physical settlement makes hold-to-expiry non-riskless)
    planted3 = synth_chain(dte_days=3.0)
    wing3 = leg_at(planted3, 1040.0, True)
    wing3.bid, wing3.ask, wing3.ltp = cheap, cheap, cheap
    assert scan_chain(planted3) == [], "delivery-week scan must return []"

    # 4) graceful degradation
    assert scan_chain(None) == []
    assert scan_chain(ChainSnapshot(symbol="X", expiry_epoch=0,
                                    lot=1, spot=100.0)) == []

    # 5) background sweep on a fake hub (fair stock must NOT appear)
    class _FakeHub:
        def __init__(self, chains):
            self._chains = chains

        def fo_universe(self):
            return sorted(self._chains)

        def chain_snapshot(self, symbol, span=8):
            return self._chains.get(symbol)

    hub = _FakeHub({"FAIRSTK": fair, "GOODSTK": planted})
    scan = ZeroLossScan(hub)
    assert scan.start(limit=1)                  # FAIRSTK only (sorted first)
    for _ in range(600):
        if not scan.status["running"]:
            break
        time.sleep(0.05)
    assert not scan.status["running"] and scan.status["error"] is None
    assert scan.status["done"] == 1 and scan.status["found"] == 0
    assert scan.results == []

    assert scan.start()                          # full sweep
    for _ in range(600):
        if not scan.status["running"]:
            break
        time.sleep(0.05)
    st = scan.status
    assert not st["running"] and st["error"] is None, st
    assert st["total"] == 2 and st["done"] == 2 and st["found"] == 1, st
    assert st["started_at"]
    assert len(scan.results) == 1
    entry = scan.results[0]
    assert entry["symbol"] == "GOODSTK"
    assert entry["spot"] == 1000.0 and entry["lot"] == 250
    assert len(entry["structures"]) >= 1
    assert entry["best_floor"] == entry["structures"][0]["floor_per_lot"]

    top = wing_hits[0]
    print(f"zeroloss.py self-test OK — fair chain: 0 hits (as required); "
          f"planted chain: {len(hits)} hits in {dt:.2f}s; best "
          f"{top['label']} floor ₹{top['floor_per_lot']:,.0f}/lot, "
          f"max profit {top['max_profit_per_lot']}, "
          f"planted wing bought at ask ₹{cheap:.2f}")
