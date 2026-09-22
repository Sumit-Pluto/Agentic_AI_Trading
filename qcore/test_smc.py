"""Verify the C++ simulate_zones kernel matches SmcZones._simulate exactly.

Run:  python -m qcore.test_smc      (or: python qcore/test_smc.py)

Checks, on a synthetic 300-bar series: (1) the batched C++ zone sim equals the
per-zone Python reference across 600 varied zones (state/mitig/weakened/inv_at),
(2) SmcZones.compute() produces the identical score via the C++ path and the
forced Python fallback, and (3) prints the batch speedup.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import qcore
from quant.agents import smc


def _mk_df(n_bars):
    """Synthetic 5m NSE-session candles (naive-IST index, oldest first)."""
    rng = np.random.default_rng(7)
    times, day0, d = [], pd.Timestamp("2026-06-29 09:15"), 0
    while len(times) < n_bars + 80:
        day = day0 + pd.Timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        for k in range(75):
            times.append(day + pd.Timedelta(minutes=5 * k))
    idx = pd.DatetimeIndex(times[-n_bars:])
    n = len(idx)
    ret = rng.normal(0, 0.0015, n)
    ret[60:66] += 0.004
    ret[120:124] -= 0.005
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.roll(close, 1)
    open_[0] = 100.0
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.0012, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.0012, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": rng.integers(1000, 9000, n),
                         "oi": 0}, index=idx)


def _arrays(zones):
    return (np.array([z["bottom"] for z in zones], dtype=float),
            np.array([z["top"] for z in zones], dtype=float),
            np.array([1 if z["pol"] == "bull" else 0 for z in zones], dtype=np.intc),
            np.array([z["formed"] for z in zones], dtype=np.intc))


def main():
    assert qcore.BACKEND == "cpp" and hasattr(qcore, "simulate_zones"), (
        "qcore C++ simulate_zones not built — run qcore/build.sh")

    df = _mk_df(300)
    sh = smc.make_shared(df)
    n = sh["n"]
    h, l = sh["h"], sh["l"]
    A = sh["atr"]
    buf = smc.INVALIDATION_BUFFER * (A if (A == A and A > 0) else 0.0)

    # A spread of candidate zones: both polarities, formed across the series,
    # widths from tight to wide, so all four end states are exercised.
    rng = np.random.default_rng(11)
    zones = []
    for _ in range(600):
        f = int(rng.integers(0, n - 1))
        mid = float((h[f] + l[f]) / 2.0)
        half = float((h[f] - l[f]) / 2.0) * float(rng.uniform(0.3, 3.0)) + 1e-6
        pol = "bull" if rng.random() < 0.5 else "bear"
        zones.append({"bottom": mid - half, "top": mid + half,
                      "pol": pol, "formed": f})

    # Python reference (per-zone _simulate on copies).
    ref = [dict(z) for z in zones]
    for z in ref:
        smc.SmcZones._simulate(z, sh)

    # C++ batched.
    bottoms, tops, is_bull, formed = _arrays(zones)
    st, mi, wk, iv = qcore.simulate_zones(
        sh["o"], sh["h"], sh["l"], sh["c"], float(buf), bottoms, tops,
        is_bull, formed, smc.MITIGATION_SEPARATION, smc.MITIGATIONS_TO_DEATH)

    names = ("FRESH", "MITIGATED", "DEAD", "INVALIDATED")
    mism = 0
    dist: dict = {}
    for i, z in enumerate(ref):
        got_state = names[int(st[i])]
        got_inv = None if int(iv[i]) < 0 else int(iv[i])
        dist[got_state] = dist.get(got_state, 0) + 1
        if (got_state != z["state"] or int(mi[i]) != z["mitig"]
                or bool(wk[i]) != z["weakened"] or got_inv != z["inv_at"]):
            mism += 1
    assert mism == 0, f"{mism}/{len(ref)} zone mismatches vs Python reference"

    # End-to-end: SmcZones score identical via C++ vs forced Python fallback.
    class _Ctx:
        def __init__(self, direction):
            self.df = df
            self.direction = direction
            self.meta = {}

    agent = smc.SmcZones()
    for direction in ("BUY", "SELL"):
        s_cpp, _ = agent.compute(_Ctx(direction))
        saved = qcore.simulate_zones
        try:
            del qcore.simulate_zones            # force the Python fallback
            s_py, _ = agent.compute(_Ctx(direction))
        finally:
            qcore.simulate_zones = saved
        assert s_cpp == s_py, (direction, s_cpp, s_py)

    # Benchmark: python per-zone loop vs one batched C++ call.
    N = 4000
    big = (zones * (N // len(zones) + 1))[:N]
    t0 = time.perf_counter()
    for z in big:
        smc.SmcZones._simulate(dict(z), sh)
    py_ms = (time.perf_counter() - t0) * 1e3
    b2, t2, ib2, f2 = _arrays(big)
    t0 = time.perf_counter()
    qcore.simulate_zones(sh["o"], sh["h"], sh["l"], sh["c"], float(buf),
                         b2, t2, ib2, f2, smc.MITIGATION_SEPARATION,
                         smc.MITIGATIONS_TO_DEATH)
    cpp_ms = (time.perf_counter() - t0) * 1e3

    print(f"qcore backend        : {qcore.BACKEND}")
    print(f"zone sims checked    : {len(ref)}  (0 mismatch vs Python)")
    print(f"end states seen      : {dist}")
    print(f"SmcZones score parity: cpp == python  OK")
    print(f"batch sim x{N}       : python {py_ms:.0f} ms  vs  cpp {cpp_ms:.1f} ms  "
          f"({py_ms / cpp_ms:.0f}x faster)")
    print("qcore SMC test: OK")


if __name__ == "__main__":
    main()
