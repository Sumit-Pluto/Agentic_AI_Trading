"""Black-Scholes greeks/IV and the gateway-payload -> Chain pipeline.

Greeks are checked against closed-form references (r=0 ATM), put-call parity and
finite differences; the chain builder is checked end-to-end on a synthetic
payload priced at a known IV+carry, so a field-name slip in the mapping or a
wrong carry (which manufactures phantom skew) fails here rather than live.
"""
import datetime as dt
import math

from intraday.options import mathx
from intraday.options.chain_builder import build_chain, greeks_for
from intraday.options.models import ist_today


# ── closed-form / analytic references ───────────────────────────────────────
def test_atm_call_price_and_delta_r0():
    # ATM, r=0: C = S(2N(sigma*sqrt(t)/2) - 1); delta = N(d1), d1 = sigma*sqrt(t)/2
    S = K = 100.0; t = 1.0; sig = 0.20
    c = mathx.bs_price(True, S, K, t, sig, r=0.0)
    assert abs(c - 7.9656) < 1e-3, c
    d = mathx.bs_delta(True, S, K, t, sig, r=0.0)
    assert abs(d - mathx.norm_cdf(0.1)) < 1e-9, d


def test_put_call_parity():
    S, K, t, sig, r = 105.0, 100.0, 0.5, 0.3, 0.07
    c = mathx.bs_price(True, S, K, t, sig, r)
    p = mathx.bs_price(False, S, K, t, sig, r)
    assert abs((c - p) - (S - K * math.exp(-r * t))) < 1e-9


def test_gamma_matches_finite_difference():
    S, K, t, sig, r = 100.0, 100.0, 0.25, 0.30, 0.07
    h = 0.01
    d_up = mathx.bs_delta(True, S + h, K, t, sig, r)
    d_dn = mathx.bs_delta(True, S - h, K, t, sig, r)
    fd = (d_up - d_dn) / (2 * h)
    assert abs(mathx.bs_gamma(S, K, t, sig, r) - fd) < 1e-4


def test_implied_vol_roundtrips():
    S, K, t, r = 100.0, 105.0, 0.4, 0.07
    for true_iv in (0.12, 0.25, 0.55):
        px = mathx.bs_price(True, S, K, t, true_iv, r)
        got = mathx.implied_vol(True, px, S, K, t, r)
        assert got is not None and abs(got - true_iv) < 1e-3, (true_iv, got)


def test_degenerate_inputs_fall_back_to_intrinsic_not_crash():
    assert mathx.bs_price(True, 100, 90, 0, 0.2) == 10.0    # expired ITM call
    assert mathx.bs_gamma(0, 100, 0.1, 0.2) == 0.0          # spot 0 -> 0, no crash
    assert mathx.implied_vol(True, -1, 100, 100, 0.1) is None


# ── gateway payload -> Chain pipeline ───────────────────────────────────────
def _synthetic_payload(spot=24800.0, dte=7, iv=0.14, r=0.07, step=100, n=5,
                       lot=50):
    expiry = ist_today() + dt.timedelta(days=dte)
    t = dte / 365.0
    atm = round(spot / step) * step
    rows = []
    for i in range(-n, n + 1):
        k = atm + i * step
        leg = {}
        for right, is_call in (("CE", True), ("PE", False)):
            mid = mathx.bs_price(is_call, spot, k, t, iv, r)
            mid = max(mid, 0.05)
            leg[right] = {"quoted": True, "strike": k,
                          "ltp": round(mid, 2),
                          "bid": round(mid * 0.99, 2), "ask": round(mid * 1.01, 2),
                          "oi_num": 100000, "prev_oi": 90000, "volume": 5000,
                          "prev_close": round(mid, 2), "lot_size": lot,
                          "token": f"TK{int(k)}{right}", "tsym": f"NIFTY{int(k)}{right}"}
        rows.append({"strike": k, **leg})
    return {"symbol": "NIFTY", "exchange": "NFO", "spot": spot,
            "expiry_iso": expiry.isoformat(), "lot_size": lot,
            "underlying_exchange": "NSE", "underlying_token": "26000",
            "chain": rows, "quality": {"quote_span_s": 2.0}}


def test_build_chain_maps_and_inverts_iv():
    true_iv, true_r = 0.14, 0.07
    ch = build_chain(_synthetic_payload(iv=true_iv, r=true_r))
    assert ch.symbol == "NIFTY" and ch.lot_size == 50
    assert len(ch.quotes) == 22            # 11 strikes * 2 rights
    # carry recovered from the chain's own put-call parity (dte<7 skips the band)
    assert ch.carry_rate is not None and abs(ch.carry_rate - true_r) < 0.02, ch.carry_rate
    # ATM IV recovered by inversion, at the calibrated carry
    atm_call = ch.get(ch.atm, True)
    assert atm_call.iv is not None and abs(atm_call.iv - true_iv) < 0.01, atm_call.iv
    # tokens/tsym carried through for order routing
    assert atm_call.token.startswith("TK") and atm_call.tsym.startswith("NIFTY")


def test_greeks_for_atm_call_is_near_half_delta():
    ch = build_chain(_synthetic_payload(iv=0.14))
    g = greeks_for(ch.get(ch.atm, True), ch)
    assert 0.45 < g["delta"] < 0.62, g          # ATM call delta ~0.5+
    assert g["gamma"] > 0 and g["vega"] > 0
    assert g["theta"] < 0                         # long option bleeds theta/day


def test_zero_priced_and_unquoted_legs_dropped():
    p = _synthetic_payload(n=2)
    p["chain"][0]["CE"]["quoted"] = False            # drop this one
    p["chain"][1]["PE"] = {"quoted": True, "strike": p["chain"][1]["strike"],
                           "ltp": 0, "bid": 0, "ask": 0}   # zero-priced -> dropped
    ch = build_chain(p)
    assert ch.get(p["chain"][0]["strike"], True) is None
    assert ch.get(p["chain"][1]["strike"], False) is None
