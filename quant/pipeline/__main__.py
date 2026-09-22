"""quant.pipeline CLI — exercise the full pipeline on synthetic data.

    python -m quant.pipeline selftest    # run the end-to-end test suite (asserts)
    python -m quant.pipeline gen         # write dummy data to data/pipeline_dummy/
    python -m quant.pipeline train       # generate + train + print the report
    python -m quant.pipeline infer       # generate + train + infer on last day
"""

from __future__ import annotations

import argparse
import json


def _report(rep):
    print(json.dumps(rep, indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m quant.pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen"); g.add_argument("--stocks", type=int, default=6)
    g.add_argument("--days", type=int, default=120)
    for name in ("train", "infer"):
        s = sub.add_parser(name)
        s.add_argument("--stocks", type=int, default=6)
        s.add_argument("--days", type=int, default=120)
        s.add_argument("--stride", type=int, default=5)
    sub.add_parser("selftest")
    a = p.parse_args(argv)

    if a.cmd == "selftest":
        from .test_pipeline import main as testmain
        testmain(); return

    from . import synthdata
    if a.cmd == "gen":
        dd = synthdata.generate(a.stocks, a.days)
        out = synthdata.persist(dd)
        print(f"wrote dummy data ({len(dd.symbols)} stocks x {len(dd.days)} days) -> {out}")
        return

    from . import pipeline as P
    dd = synthdata.generate(a.stocks, a.days)
    res = P.train(dd, stride=a.stride)
    print("\n=== TRAIN REPORT ==="); _report(res["report"])
    if a.cmd == "infer":
        last_day = dd.days[-1]
        print(f"\n=== INFER (day {last_day}, filters ON) ===")
        out_on = P.infer(dd, res["model"], res["bank"], day=last_day)
        print(f"candidates={out_on['n_candidates']} vetoed={out_on['n_vetoed']} "
              f"filters={out_on['filters_enabled']}")
        for c in out_on["chosen"]:
            print(f"  {c.event.symbol} {c.event.direction} via {c.event.strategy_id} "
                  f"P(win)={c.p_win:.2f} size={c.size}")
        # demonstrate a filter toggle
        cfg_off = {"regime_gate": {"enabled": False}, "ban_gate": {"enabled": False}}
        out_off = P.infer(dd, res["model"], res["bank"], filter_config=cfg_off, day=last_day)
        print(f"\n(with regime_gate+ban_gate OFF) vetoed={out_off['n_vetoed']} "
              f"filters={out_off['filters_enabled']}")


if __name__ == "__main__":
    main()
