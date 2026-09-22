"""Offline self-test for quant.training — no broker, no network.

Run:  python -m quant.training.test_training
Covers: tree flattening, collector round-trip, horizon labeling, dataset build,
the trainer's signal recovery, and its data guardrails.
"""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pandas as pd

from quant.training.collector import (TrainingCollector, flatten_families,
                                      flatten_leaves, FAMILIES)
from quant.training import dataset as ds
from quant.training import trainer as tr
from quant.training.labeler import label_samples


def _fake_tree(smc, snr, vol, volu, macro):
    """A result['tree'] shaped like Scanner.evaluate() produces."""
    def branch(key, score, leaves):
        return {"key": key, "score": score,
                "children": [{"key": f"{key}_{i}", "score": s, "children": []}
                             for i, s in enumerate(leaves)]}
    return {"key": "quant", "score": 60.0, "children": [
        branch("smc", smc, [smc, smc - 5]),
        branch("snr", snr, [snr]),
        branch("volatility", vol, [vol]),
        branch("volume", volu, [volu]),
        {"key": "macro", "score": macro, "children": []} if macro is not None
        else {"key": "macro", "score": None, "children": []},
    ]}


def test_flatten():
    tree = _fake_tree(57.2, 61.0, 70.1, 48.0, None)
    fam = flatten_families(tree)
    assert fam == {"smc": 57.2, "snr": 61.0, "volatility": 70.1,
                   "volume": 48.0, "macro": None}, fam
    leaves = flatten_leaves(tree)
    assert leaves["smc_0"] == 57.2 and leaves["smc_1"] == 52.2, leaves
    assert flatten_families(None)["smc"] is None
    print("ok  flatten families/leaves")


def test_collector_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "samples.jsonl")
        c = TrainingCollector(path)
        res = {"symbol": "TCS", "direction": "BUY", "segment": "FNO",
               "price": 100.0, "score": 64.9, "accepted": True,
               "tree": _fake_tree(57, 61, 70, 48, None),
               "evaluated_at": "2026-07-06T13:30:00",
               "trigger": {"bar_time": "2026-07-06T13:30:00"}}
        c.record(res, "agent_primary")
        c.record({"symbol": "X", "direction": "BUY", "score": None}, "x")  # skip
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert len(rows) == 1, rows                      # the None-score was skipped
        r = rows[0]
        assert r["families"]["volatility"] == 70.0 and r["date"] == "2026-07-06"
        assert r["labeled"] is False
        print("ok  collector round-trip (None-score skipped)")


def test_horizon_labeling():
    # a rising day: BUY entered at bar 5 should win over a 12-bar horizon
    idx = pd.date_range("2026-07-06 09:15", periods=40, freq="5min")
    close = pd.Series(np.linspace(100, 140, 40), index=idx)
    day = pd.DataFrame({"open": close, "high": close + 1,
                        "low": close - 1, "close": close, "volume": 1000})

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "samples.jsonl")
        with open(path, "w") as f:
            for direction in ("BUY", "SELL"):
                f.write(json.dumps({
                    "symbol": "AAA", "date": "2026-07-06",
                    "bar_time": "2026-07-06 09:40:00", "direction": direction,
                    "price": float(close.iloc[5]), "source": "agent_primary",
                    "families": {"smc": 60}, "labeled": False}) + "\n")

        provider = lambda sym, date: day if date == "2026-07-06" else None
        summ = label_samples(provider, path, method="horizon",
                             min_forward_bars=6, horizon_bars=12)
        assert summ["labeled_now"] == 2, summ
        rows = {r["direction"]: r for r in
                (json.loads(l) for l in open(path) if l.strip())}
        assert rows["BUY"]["label"] == 1, rows["BUY"]      # up day -> BUY wins
        assert rows["SELL"]["label"] == 0, rows["SELL"]    #          -> SELL loses
        assert rows["BUY"]["outcome_pnl"] > 0
        print("ok  horizon labeling (BUY win / SELL loss on an up day)")


def _synth_samples(path, n=500, seed=0, days=40):
    """volume score drives the outcome; the rest are noise -> the trainer must
    hand 'volume' the dominant weight. Dates are spread across `days` so the
    walk-forward validator has time-ordered folds to work with."""
    rng = np.random.default_rng(seed)
    dts = pd.date_range("2026-06-01 10:00", periods=days, freq="D")
    with open(path, "w") as f:
        for i in range(n):
            vol = float(rng.uniform(0, 100))
            fam = {"smc": float(rng.uniform(0, 100)),
                   "snr": float(rng.uniform(0, 100)),
                   "volatility": float(rng.uniform(0, 100)),
                   "volume": vol,
                   "macro": float(rng.uniform(0, 100))}
            # win probability rises with the volume score, plus noise
            p = 1.0 / (1.0 + np.exp(-(vol - 50) / 12.0))
            label = int(rng.uniform() < p)
            bt = dts[i % days]
            f.write(json.dumps({
                "symbol": "S", "date": str(bt.date()),
                "bar_time": bt.isoformat(), "direction": "BUY",
                "source": "agent_primary", "accepted": True,
                "families": fam, "labeled": True, "label": label}) + "\n")


def test_trainer_recovers_signal():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "samples.jsonl")
        _synth_samples(path, n=600)
        data = ds.build(path)
        assert data["BUY"].n == 600, data["BUY"].n
        results = tr.train(data, defaults=None, min_samples=200)
        buy = results["BUY"]
        assert buy.trained, buy.reason
        top = max(FAMILIES, key=lambda k: buy.weights.get(k, 0))
        assert top == "volume", (top, buy.weights)
        assert buy.metrics["auc"] > 0.6, buy.metrics
        # walk-forward must run and the learned model must beat equal-weights OOS
        assert buy.oos.get("folds", 0) > 0, buy.oos
        assert buy.beats_baseline and buy.ship, buy.oos
        print(f"ok  trainer recovered 'volume' as top weight "
              f"{buy.weights} (in-sample auc={buy.metrics['auc']}, "
              f"OOS model auc={buy.oos['model_auc']} vs baseline "
              f"{buy.oos['baseline_auc']} -> SHIP)")


def test_guardrail_blocks_tiny_data():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "samples.jsonl")
        _synth_samples(path, n=40)
        data = ds.build(path)
        results = tr.train(data, defaults=None, min_samples=200)
        assert not results["BUY"].trained
        assert "need >= 200" in results["BUY"].reason, results["BUY"].reason
        print("ok  guardrail refuses to train on 40 samples")


def _write_synth_dhan(dhan_dir, symbol="TESTX", days=4, seed=1):
    """Tiny synthetic dhan intraday1m_<SYM>.json: `days` NSE sessions of 1-min
    OHLCV. Epochs are UTC (09:15 IST == 03:45 UTC) so replay_hub reconstructs
    the IST trading clock."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in range(days):
        start = pd.Timestamp("2026-07-06 09:15") + pd.Timedelta(days=d)
        idx = pd.date_range(start, periods=375, freq="1min")   # 09:15..15:29
        steps = rng.normal(0, 0.5, len(idx)).cumsum()
        close = 1000 + steps + d * 2
        frames.append(pd.DataFrame({"close": close}, index=idx))
    df = pd.concat(frames)
    close = df["close"].to_numpy()
    epoch = (df.index.tz_localize("Asia/Kolkata").tz_convert("UTC")
             .view("int64") // 10**9).tolist()
    obj = {"open": (close - 0.2).tolist(), "high": (close + 1).tolist(),
           "low": (close - 1).tolist(), "close": close.tolist(),
           "volume": [10000.0] * len(close), "timestamp": epoch}
    os.makedirs(dhan_dir, exist_ok=True)
    with open(os.path.join(dhan_dir, f"intraday1m_{symbol}.json"), "w") as f:
        json.dump(obj, f)


def test_replay_backtest():
    from quant.training.replay import run_replay
    with tempfile.TemporaryDirectory() as d:
        dhan = os.path.join(d, "dhan")
        _write_synth_dhan(dhan, "TESTX", days=4)
        out = os.path.join(d, "samples.jsonl")
        summ = run_replay(dhan, None, out, stride=6, fresh=True, progress=None)
        assert summ["written"] > 0, summ
        rows = [json.loads(l) for l in open(out) if l.strip()]
        r = rows[0]
        assert r["source"] == "backtest" and r["labeled"] is True
        assert "smc" in r["families"]                    # candle agent scored
        assert r["label"] in (0, 1) and r["exit_reason"]
        print(f"ok  replay backtest produced {summ['written']} labeled samples "
              f"(win rate {summ['win_rate']}%, smc avail proves tree ran)")


def main():
    test_flatten()
    test_collector_roundtrip()
    test_horizon_labeling()
    test_trainer_recovers_signal()
    test_guardrail_blocks_tiny_data()
    test_replay_backtest()
    print("\nALL TRAINING TESTS PASSED")


if __name__ == "__main__":
    main()
