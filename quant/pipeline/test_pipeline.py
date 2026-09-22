"""Offline end-to-end test for quant.pipeline — no broker, no paid data.

Run: python -m quant.pipeline.test_pipeline   (or  python -m quant.pipeline selftest)
Proves: synth data + injected signal -> features (incl. option-chain) -> triple-barrier
labels correlate with the injected edge -> LightGBM meta-model recovers it out-of-sample
-> filters toggle -> inference ranks candidates.
"""

from __future__ import annotations

import numpy as np

from . import synthdata, strategies, features as F, pipeline as P
from .contracts import Candidate, TriggerEvent
from .labeler import label_event
from .filters import FilterStack


def test_synth():
    dd = synthdata.generate(n_stocks=4, n_days=30)
    assert len(dd.symbols) == 4
    s0 = dd.symbols[0]
    assert dd.candles[s0].shape[1] == 5 and len(dd.candles[s0]) > 30 * 70
    assert (s0, dd.days[0]) in dd.chain
    ch = dd.chain[(s0, dd.days[0])]["strikes"]
    assert {"strike", "ce_oi", "pe_oi", "ce_iv", "pe_iv"} <= set(ch.columns)
    edges = [m["edge"] for m in dd.meta.values()]
    assert set(edges) <= {-1, 0, 1} and len(set(edges)) > 1
    print("ok  synth: candles + per-strike chain + edge distribution")


def test_features():
    dd = synthdata.generate(n_stocks=3, n_days=20)
    bank = F.FeatureBank(dd)
    ev = strategies.grid(dd, bank, stride=20)[10]
    f = F.enrich(ev, bank, dd)
    for k in ("vix", "pcr_oi", "dist_call_wall_atr", "gex", "atm_iv", "skew_25",
              "rel_strength", "dte", "strategy_id"):
        assert k in f, k
    assert f["atm_iv"] > 0 and f["pcr_oi"] > 0
    print("ok  features: candle + regime + computed option-chain (PCR/GEX/IV/skew) present")


def test_labeler_tracks_edge():
    dd = synthdata.generate(n_stocks=5, n_days=40, seed=3)
    bank = F.FeatureBank(dd)
    up_buy, range_buy = [], []
    for ev in strategies.grid(dd, bank, stride=4):
        if ev.direction != "BUY":
            continue
        lab = label_event(ev, dd, bank)
        if lab is None:
            continue
        edge = dd.meta[(ev.symbol, ev.bar_time[:10])]["edge"]
        (up_buy if edge > 0 else range_buy if edge == 0 else []).append(lab[0])
    wr_up = np.mean(up_buy); wr_range = np.mean(range_buy)
    assert wr_up > wr_range + 0.05, (wr_up, wr_range)
    print(f"ok  labeler: BUY win-rate up-edge {wr_up:.2f} > range {wr_range:.2f} (signal is in the labels)")


def test_training_recovers_signal():
    dd = synthdata.generate(n_stocks=6, n_days=120, seed=7)
    res = P.train(dd, progress=None)
    rep = res["report"]
    assert rep["samples"] > 3000, rep["samples"]
    assert rep["holdout"]["auc"] > 0.55, rep["holdout"]
    wf = rep["walk_forward"]
    assert wf["folds"] > 0 and wf["oos_auc"] > 0.55 and wf["beats"], wf
    top = list(rep["top_features"])[:8]
    assert any(k in top for k in ("rel_strength", "vix", "dist_call_wall_atr",
                                  "dist_put_wall_atr", "dir_sign", "trend")), top
    print(f"ok  training: {rep['samples']} samples, holdout AUC {rep['holdout']['auc']}, "
          f"walk-forward OOS AUC {wf['oos_auc']} (SHIP); top features {top[:5]}")
    return res, dd


def test_filters_toggle():
    dd = synthdata.generate(n_stocks=3, n_days=20)
    bank = F.FeatureBank(dd)
    ev = TriggerEvent("grid", "grid", dd.symbols[0], "BUY",
                      strategies.grid(dd, bank, stride=30)[5].bar_time, 100.0)
    c = Candidate(event=ev, features=F.enrich(ev, bank, dd))
    c.p_win = 0.9
    c.features["ban"] = 1.0                    # force a ban
    on = FilterStack(); log_on = on.apply(c)
    assert c.vetoed_by == "ban_gate", (c.vetoed_by, log_on)
    c2 = Candidate(event=ev, features=dict(c.features)); c2.p_win = 0.9
    off = FilterStack({"ban_gate": {"enabled": False}})
    assert "ban_gate" not in off.enabled_ids
    off.apply(c2)
    assert c2.vetoed_by != "ban_gate"
    print(f"ok  filters: ban_gate vetoes when ON, skipped when OFF (enabled={off.enabled_ids})")


def test_inference(res_dd=None):
    if res_dd is None:
        dd = synthdata.generate(n_stocks=6, n_days=120, seed=7)
        res = P.train(dd, progress=None)
    else:
        res, dd = res_dd
    out = P.infer(dd, res["model"], res["bank"], day=dd.days[-1], top_k=5)
    assert out["n_candidates"] > 0
    for c in out["chosen"]:
        assert 0.0 <= c.p_win <= 1.0 and c.size >= 0
    print(f"ok  inference: {out['n_candidates']} candidates, {out['n_vetoed']} vetoed, "
          f"{len(out['chosen'])} selected (top P(win) shown by CLI)")


def main():
    test_synth()
    test_features()
    test_labeler_tracks_edge()
    res_dd = test_training_recovers_signal()
    test_filters_toggle()
    test_inference(res_dd)
    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
