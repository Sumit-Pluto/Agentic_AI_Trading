"""Verify the swing package: vendored engines run, and the Rbknox-at-OB fusion
routes correctly. Run:  python -m signals.swing.test_swing

The three engines are byte-identical to the SM Agent source (proven at vendor
time), so this focuses on (1) they import + run on Shoonya-shaped candles, and
(2) the NEW fusion logic — Knoxville reversal gating the OB context — which is
isolated with monkeypatched engines so it is deterministic.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np
import pandas as pd

from signals.swing import engine as se
from signals.swing import market_structure, ob_reversal
from signals.swing.knoxville import (KnoxParams, KnoxResult, compute_knoxville,
                                     min_bars_required)


def _df(n=280, last_close=100.0):     # > min_bars_required(KnoxParams())=233
    rng = np.random.default_rng(3)
    close = last_close + np.cumsum(rng.normal(0, 0.5, n))
    close = close - close[-1] + last_close          # force last close
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + rng.uniform(0, 0.5, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.5, n)
    vol = rng.integers(1000, 5000, n).astype(float)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def _fake_ob(status="retracing", side="LONG", top=101.0, bot=99.0, tap=5):
    return {"status": status, "side": side, "tap_idx": tap, "ob_top": top,
            "ob_bottom": bot, "entry": None, "stop": 98.0, "target": 110.0,
            "rr": 3.0, "move_pct": 2.5, "reasons": ["breakout 3.0× vol spike"]}


def main():
    df = _df()

    # (1) integration: the vendored engines run on candle-shaped data.
    struct = market_structure.analyze(df)
    assert isinstance(struct["pivots"], list)
    ob = ob_reversal.detect(df)
    assert ob is None or isinstance(ob, dict)
    kr = compute_knoxville(df, KnoxParams())
    assert isinstance(kr, KnoxResult)
    sig = se.swing_signal(df)
    assert sig is None or isinstance(sig, se.SwingCandidate)
    # default lookback=200 -> 200 + max(mom_len 20, rsi_len 14, st_rsi_len+st_len 28) + 5
    assert min_bars_required(KnoxParams()) == 233

    # (2) fusion logic, isolated with monkeypatched engines.
    orig_ob, orig_kx = se.ob_detect, se.compute_knoxville
    bull = KnoxResult(True, False, True, False, 100.0, 25.0, 10.0, 15.0)
    bear = KnoxResult(False, True, False, True, 100.0, 75.0, 90.0, 85.0)
    flat = KnoxResult(False, False, False, False, 100.0, 50.0, 50.0, 50.0)
    try:
        # LONG retracing OB + bullish KD + price in the zone → BUY
        se.ob_detect = lambda d, p=None: _fake_ob()
        se.compute_knoxville = lambda d, p: bull
        s = se.swing_signal(df, symbol="X", segment="FNO", interval="1d")
        assert s and s.direction == "BUY" and s.ob_top == 101.0 and s.target == 110.0, s
        assert s.reasons[-1] == "Rbknox bullish reversal at OB"
        assert s.to_dict()["detail"].startswith("BUY swing")

        # no bullish KD → None (this is the whole point of decision #2)
        se.compute_knoxville = lambda d, p: flat
        assert se.swing_signal(df) is None

        # price far from the OB zone → None
        se.ob_detect = lambda d, p=None: _fake_ob(top=201.0, bot=200.0)
        se.compute_knoxville = lambda d, p: bull
        assert se.swing_signal(df) is None

        # OB not yet tapped (status 'watch') → None
        se.ob_detect = lambda d, p=None: {**_fake_ob(status="watch"), "tap_idx": None}
        assert se.swing_signal(df) is None

        # SHORT setup + bearish KD → SELL
        se.ob_detect = lambda d, p=None: _fake_ob(side="SHORT")
        se.compute_knoxville = lambda d, p: bear
        s = se.swing_signal(df)
        assert s and s.direction == "SELL", s
    finally:
        se.ob_detect, se.compute_knoxville = orig_ob, orig_kx

    print("swing fusion test: OK "
          "(engines vendored byte-identical; fusion routing verified)")


if __name__ == "__main__":
    main()
