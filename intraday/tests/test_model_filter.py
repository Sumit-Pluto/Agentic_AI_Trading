"""ModelFilter: loads state/model_full.pkl, scores a live-shaped signal, and
gates entries in the loop. Skips gracefully if lightgbm/the pickle are absent."""
import datetime as dt
import os

import pandas as pd
import pytest

from intraday.contracts import Signal
from intraday.model import ModelFilter
from intraday.options import ist_now
from intraday.options.chain_builder import build_chain
from intraday.tests.test_options_math import _synthetic_payload

_MODEL = os.path.join(os.path.dirname(__file__), "..", "..", "state", "model_full.pkl")
_HAVE_MODEL = os.path.exists(_MODEL)


def _bars(n=40, start=24000.0):
    rows, prev = [], start
    for i in range(n):
        c = start + i * 8
        rows.append({"time": i, "open": prev, "high": max(prev, c) + 3,
                     "low": min(prev, c) - 3, "close": c, "volume": 12000,
                     "prev_close": prev, "vwap": (prev + c) / 2})
        prev = c
    return pd.DataFrame(rows)


def _sig(direction="BUY"):
    return Signal(ts=ist_now(), symbol="NIFTY", direction=direction, score_buy=70,
                  score_sell=20, family_scores={}, agent_rows=[], vetoes=[], n_scored=5,
                  regime={"on": True, "scalar": 1.0}, instrument={"strategy": "orb"})


def test_disabled_filter_never_blocks():
    mf = ModelFilter.maybe({"model_filter_enabled": False})
    assert mf.enabled is False
    assert mf.passes(None) is True                 # unscorable / disabled → allowed
    assert mf.win_prob(_sig(), None, None, None, None, ist_now()) is None


def test_missing_model_disables_gracefully(tmp_path):
    mf = ModelFilter.maybe({"model_filter_enabled": True,
                            "model_path": str(tmp_path / "nope.pkl")})
    assert mf.enabled is False and mf.passes(None) is True


@pytest.mark.skipif(not _HAVE_MODEL, reason="state/model_full.pkl not present")
def test_real_model_scores_and_gates():
    mf = ModelFilter.maybe({"model_filter_enabled": True, "model_path": _MODEL,
                            "model_filter_min_prob": 0.5})
    if not mf.enabled:
        pytest.skip("lightgbm/scipy unavailable to unpickle the model")
    ch = build_chain(_synthetic_payload(spot=24800, iv=0.13))
    p = mf.win_prob(_sig(), ch, _bars(), _bars(), 12.5, ist_now())
    assert p is not None and 0.0 <= p <= 1.0
    # threshold logic
    mf.min_prob = 1.01
    assert mf.passes(p) is False                    # nothing clears an impossible bar
    mf.min_prob = 0.0
    assert mf.passes(p) is True
