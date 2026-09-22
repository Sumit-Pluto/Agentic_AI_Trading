"""Verify the compiled qcore kernel matches the Python reference, and bench it.

Run:  python -m qcore.test_qcore      (or: python qcore/test_qcore.py)

Asserts the C++ backend is actually loaded (fails loudly if not built), then
checks bs_price/bs_gamma/bs_vega/implied_vol against quant.mathutils across a
grid, checks the batched implied_vols vs the scalar path, and prints the IV
inversion speedup.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import qcore
from quant import mathutils as ref


def _close(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * (1.0 + abs(b))


def main():
    assert qcore.BACKEND == "cpp", (
        f"qcore backend is {qcore.BACKEND!r}, not 'cpp' — run qcore/build.sh")

    spots = [100.0, 250.0, 1000.0, 21500.0]
    moneyness = [0.85, 0.95, 1.0, 1.05, 1.15]
    tenors = [1 / 365, 7 / 365, 30 / 365, 0.25]
    sigmas = [0.08, 0.15, 0.30, 0.55]

    n = 0
    max_price_err = max_gamma_err = max_vega_err = 0.0
    iv_checked = iv_none = 0
    max_iv_err = 0.0

    for is_call in (True, False):
        for spot in spots:
            for m in moneyness:
                strike = round(spot * m, 2)
                for t in tenors:
                    for sig in sigmas:
                        n += 1
                        p_ref = ref.bs_price(is_call, spot, strike, t, sig)
                        p_cpp = qcore.bs_price(is_call, spot, strike, t, sig)
                        g_ref = ref.bs_gamma(spot, strike, t, sig)
                        g_cpp = qcore.bs_gamma(spot, strike, t, sig)
                        v_ref = ref.bs_vega(spot, strike, t, sig)
                        v_cpp = qcore.bs_vega(spot, strike, t, sig)
                        assert _close(p_ref, p_cpp), (p_ref, p_cpp)
                        assert _close(g_ref, g_cpp), (g_ref, g_cpp)
                        assert _close(v_ref, v_cpp), (v_ref, v_cpp)
                        max_price_err = max(max_price_err, abs(p_ref - p_cpp))
                        max_gamma_err = max(max_gamma_err, abs(g_ref - g_cpp))
                        max_vega_err = max(max_vega_err, abs(v_ref - v_cpp))

                        # Invert the just-priced premium and compare IV solves.
                        iv_ref = ref.implied_vol(is_call, p_ref, spot, strike, t)
                        iv_cpp = qcore.implied_vol(is_call, p_ref, spot, strike, t)
                        assert _close(iv_ref, iv_cpp, tol=1e-6), (iv_ref, iv_cpp)
                        iv_checked += 1
                        if iv_ref is None:
                            iv_none += 1
                        else:
                            max_iv_err = max(max_iv_err, abs(iv_ref - iv_cpp))

    # Batched implied_vols vs scalar (incl. a NaN/no-solution leg).
    price = np.array([12.5, 40.0, 0.0, 3.2], dtype=float)     # 0.0 -> NaN
    spot = np.array([21500.0, 21500.0, 21500.0, 21500.0], dtype=float)
    strike = np.array([21500.0, 21000.0, 22000.0, 21800.0], dtype=float)
    batch = qcore.implied_vols(True, price, spot, strike, 30 / 365)
    for i in range(len(price)):
        one = qcore.implied_vol(True, price[i], spot[i], strike[i], 30 / 365)
        if one is None:
            assert np.isnan(batch[i]), (i, batch[i])
        else:
            assert _close(one, float(batch[i]), tol=1e-12), (i, one, batch[i])

    # Benchmark: many IV inversions, Python loop vs C++ batch (GIL released).
    N = 20000
    rng = np.random.default_rng(0)
    bp = rng.uniform(5, 500, N)
    bs = np.full(N, 21500.0)
    bk = rng.uniform(20000, 23000, N)
    t0 = time.perf_counter()
    for i in range(N):
        ref.implied_vol(True, bp[i], bs[i], bk[i], 30 / 365)
    py_ms = (time.perf_counter() - t0) * 1e3
    t0 = time.perf_counter()
    qcore.implied_vols(True, bp, bs, bk, 30 / 365)
    cpp_ms = (time.perf_counter() - t0) * 1e3

    print(f"qcore backend        : {qcore.BACKEND}")
    print(f"grid cases           : {n} priced, {iv_checked} IV solves "
          f"({iv_none} out-of-bounds -> None on both)")
    print(f"max |Δ| price/gamma/vega: {max_price_err:.2e} / "
          f"{max_gamma_err:.2e} / {max_vega_err:.2e}")
    print(f"max |Δ| implied vol  : {max_iv_err:.2e}")
    print(f"IV inversion x{N}    : python {py_ms:.0f} ms  vs  "
          f"cpp {cpp_ms:.0f} ms  ({py_ms / cpp_ms:.1f}x faster)")
    print("qcore test: OK")


if __name__ == "__main__":
    main()
