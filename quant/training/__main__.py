"""quant/training CLI — run the forward-collection training pipeline by hand.

    python -m quant.training status
        How many samples collected / labeled, per direction, per source.

    python -m quant.training label --csv-dir data/history [--method exit_walk]
        Attach forward win/loss outcomes to collected samples using 5-minute
        candle CSVs (fetch_history.py output). Idempotent — re-run each session.

    python -m quant.training train [--sources agent_primary] [--min-samples 200]
        Fit per-direction weights and PRINT the report. Writes nothing.

    python -m quant.training apply  [same flags as train] [--yes]
        Same fit, then write the trained weights into quant_config.json
        (backs up the old file first). Refuses directions with too little data.

    python -m quant.training report
        status + a train dry-run in one view.

Typical loop: run the app so the agent scanner collects samples over some
sessions -> `label --csv-dir data/history` after fetching 5m candles ->
`train` to inspect -> `apply --yes` once the numbers look real.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime

from .collector import FAMILIES, DEFAULT_PATH
from . import dataset as ds
from . import trainer as tr


# ── pretty printers ──────────────────────────────────────────────────────────
def _print_status(path: str) -> None:
    s = ds.status(path)
    if not s.get("exists"):
        print(f"no samples yet at {s['path']}\n"
              "  run the app (agent scanner) with AGENTS_TRAINING_COLLECT=1 to "
              "start collecting.")
        return
    print(f"samples file : {s['path']}")
    print(f"collected    : {s['total']}  "
          f"(BUY {s['by_direction'].get('BUY', 0)}, "
          f"SELL {s['by_direction'].get('SELL', 0)})")
    print(f"labeled      : {s['labeled']}   pending: {s['pending']}")
    if s["labeled"]:
        print(f"outcomes     : {s['wins']} wins / {s['losses']} losses "
              f"(win rate {s['win_rate']}%)")
    if s.get("date_range"):
        print(f"date range   : {s['date_range'][0]} .. {s['date_range'][1]} "
              f"({s['days']} day(s))")
    print(f"by source    : {s['by_source']}")


def _print_result(r: tr.DirResult) -> None:
    print(f"\n── {r.direction} ──  n={r.n}  win_rate={r.win_rate:.1%}"
          if r.n else f"\n── {r.direction} ──  n=0")
    if not r.trained:
        print(f"   NOT TRAINED: {r.reason}")
        print(f"   keeping weights: "
              + ", ".join(f"{k}={r.prev_weights.get(k, 1.0):g}"
                          for k in FAMILIES))
        return
    m = r.metrics
    print(f"   in-sample: acc={m['accuracy']:.3f}  auc={m['auc']}  "
          f"log_loss={m['log_loss']}  base_rate={m['base_rate']:.3f}")
    o = r.oos or {}
    if o.get("folds"):
        print(f"   walk-forward ({o['folds']} folds, n_oos={o['n_oos']}): "
              f"model auc={o['model_auc']}  vs  current-config auc="
              f"{o['baseline_auc']}")
    else:
        print(f"   walk-forward: {o.get('reason', 'n/a')}")
    verdict = ("SHIP — beats current config OOS" if r.ship
               else "HOLD — does NOT beat current config OOS "
                    "(apply will keep defaults)")
    print(f"   verdict: {verdict}")
    print(f"   {'family':<12}{'avail':>7}{'coef':>9}{'  before':>9}{'  after':>9}")
    for k in FAMILIES:
        av = r.availability.get(k, 0.0)
        print(f"   {k:<12}{av*100:>6.0f}%{r.coefs.get(k, 0.0):>9.3f}"
              f"{r.prev_weights.get(k, 1.0):>9.2f}{r.weights.get(k, 0.0):>9.2f}")


def _run_train(args):
    sources = set(args.sources.split(",")) if args.sources else None
    data = ds.build(args.path, sources=sources, accepted_only=args.accepted_only)
    defaults = tr.current_default_weights()
    results = tr.train(data, defaults=defaults, l2=args.l2,
                       min_samples=args.min_samples, floor=args.floor)
    print(f"\n=== TRAIN (sources={sources or 'all'}, "
          f"accepted_only={args.accepted_only}) ===")
    for direction in ("BUY", "SELL"):
        _print_result(results[direction])
    return results


# ── config write ─────────────────────────────────────────────────────────────
def _apply(results, path: str) -> None:
    from quant.config import QuantConfig, CONFIG_PATH
    if os.path.exists(CONFIG_PATH):
        bak = CONFIG_PATH + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(CONFIG_PATH, bak)
        print(f"\nbacked up {CONFIG_PATH} -> {bak}")
    cfg = QuantConfig()
    n_written = 0
    for direction in ("BUY", "SELL"):
        r = results[direction]
        if not r.ship:                       # baseline gate — not just a fit
            print(f"skip {direction}: {r.reason}")
            continue
        for key in FAMILIES:
            if key in r.weights:
                cfg.set_weight(key, direction, float(r.weights[key]))
                n_written += 1
        print(f"applied {direction}: "
              + ", ".join(f"{k}={r.weights[k]:g}" for k in FAMILIES
                          if k in r.weights))
    print(f"wrote {n_written} weight(s) into {CONFIG_PATH}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m quant.training", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", default=DEFAULT_PATH,
                   help=f"samples JSONL (default {DEFAULT_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="counts of collected / labeled samples")

    pb = sub.add_parser("bootstrap",
                        help="seed samples from an existing paper_trades.jsonl")
    pb.add_argument("paper", help="path to a paper_trades.jsonl")
    pb.add_argument("--cost", type=float, default=0.0)

    pr = sub.add_parser("replay",
                        help="generate training samples by replaying history")
    pr.add_argument("--dhan-dir", default=None,
                    help="dir of intraday1m_<SYM>.json files (Hyena_X dhan)")
    pr.add_argument("--cache-dir", default=None,
                    help="parquet cache dir (hf_data.py output) — use instead of --dhan-dir")
    pr.add_argument("--vix", default=None, help="path to vix_daily.json")
    pr.add_argument("--symbols", default=None, help="comma list (default: all)")
    pr.add_argument("--start", default=None, help="YYYY-MM-DD (bars >= start)")
    pr.add_argument("--end", default=None, help="YYYY-MM-DD (bars < end)")
    pr.add_argument("--stride", type=int, default=3,
                    help="sample every Nth 5m bar (default 3 = ~15 min)")
    pr.add_argument("--prefix-bars", type=int, default=400)
    pr.add_argument("--min-forward-bars", type=int, default=6)
    pr.add_argument("--cost", type=float, default=0.0)
    pr.add_argument("--limit", type=int, default=0, help="cap #symbols")
    pr.add_argument("--fresh", action="store_true",
                    help="truncate the samples file before writing")

    pt = sub.add_parser("train-per-symbol",
                        help="fit PER-STOCK weights (shrunk to a global prior)")
    pt.add_argument("--sources", default="backtest")
    pt.add_argument("--min-samples", type=int, default=tr.MIN_SAMPLES)
    pt.add_argument("--l2", type=float, default=1.0)
    pt.add_argument("--out", default="weights_3stocks.json",
                    help="config file to write per_symbol weights into")
    pt.add_argument("--apply", action="store_true",
                    help="write the per-stock weights to --out (else dry-run)")
    pt.add_argument("--force", action="store_true",
                    help="write fitted weights even if they don't beat default "
                         "OOS (for the trained-vs-default backtest experiment)")

    pbt = sub.add_parser("backtest",
                         help="out-of-sample backtest: trained vs default weights")
    pbt.add_argument("--weights", default="weights_3stocks.json",
                     help="trained per_symbol config file")
    pbt.add_argument("--symbols", default=None, help="comma list (default: cached)")
    pbt.add_argument("--start", required=True, help="YYYY-MM-DD (test start)")
    pbt.add_argument("--end", required=True, help="YYYY-MM-DD (test end)")
    pbt.add_argument("--modes", default="confirm,agents")
    pbt.add_argument("--stride", type=int, default=3)
    pbt.add_argument("--cache-dir", default="data/hf_cache")
    pbt.add_argument("--vix", default=None)
    pbt.add_argument("--save", default="backtest_result.json",
                     help="write the metrics summary here")

    pl = sub.add_parser("label", help="attach forward win/loss outcomes")
    pl.add_argument("--csv-dir", required=True,
                    help="dir of 5m candle CSVs (fetch_history.py output)")
    pl.add_argument("--method", choices=["exit_walk", "horizon"],
                    default="exit_walk")
    pl.add_argument("--min-forward-bars", type=int, default=6)
    pl.add_argument("--horizon-bars", type=int, default=12)
    pl.add_argument("--cost", type=float, default=0.0,
                    help="per-share cost floor a win must clear")

    for name in ("train", "apply", "report"):
        sp = sub.add_parser(name)
        if name in ("train", "apply"):
            sp.add_argument("--sources", default=None,
                            help="comma list, e.g. agent_primary (default all)")
            sp.add_argument("--accepted-only", action="store_true")
            sp.add_argument("--min-samples", type=int, default=tr.MIN_SAMPLES)
            sp.add_argument("--l2", type=float, default=1.0)
            sp.add_argument("--floor", type=float, default=tr.WEIGHT_FLOOR)
        if name == "apply":
            sp.add_argument("--yes", action="store_true",
                            help="write without the confirm prompt")

    args = p.parse_args(argv)

    if args.cmd == "status":
        _print_status(args.path)
        return

    if args.cmd == "bootstrap":
        from .bootstrap import import_paper_trades
        summ = import_paper_trades(args.paper, args.path, cost=args.cost)
        if summ.get("error"):
            print(summ["error"])
            return
        print(f"imported {summ['written']} labeled sample(s) from "
              f"{summ['closed_positions']} closed position(s): "
              f"{summ['wins']} wins / {summ['losses']} losses "
              f"({summ['unmatched_entries']} entries had no matching exit)")
        return

    if args.cmd == "label":
        from .labeler import label_samples, CsvCandlesProvider
        provider = CsvCandlesProvider(args.csv_dir)
        summary = label_samples(provider, args.path, method=args.method,
                                min_forward_bars=args.min_forward_bars,
                                horizon_bars=args.horizon_bars, cost=args.cost)
        print(f"labeled now : {summary['labeled_now']} "
              f"({summary['wins']} wins / {summary['losses']} losses)")
        print(f"still pending: {summary['still_pending']}  "
              f"(no candles for {summary['no_candles']})")
        return

    if args.cmd == "replay":
        from .replay import run_replay
        syms = args.symbols.split(",") if args.symbols else None
        hub = None
        if args.cache_dir:
            from .replay_hub import ReplayHub
            hub = ReplayHub.from_parquet_cache(args.cache_dir, symbols=syms,
                                               vix_path=args.vix)
        summ = run_replay(args.dhan_dir, args.vix, args.path, hub=hub, symbols=syms,
                          stride=args.stride, prefix_bars=args.prefix_bars,
                          min_forward_bars=args.min_forward_bars, cost=args.cost,
                          limit=args.limit, start=args.start, end=args.end,
                          fresh=args.fresh)
        if summ.get("error"):
            print(summ["error"])
            return
        print(f"\nreplay done: {summ['written']} samples from "
              f"{summ['symbols']} symbol(s) — {summ['wins']} wins / "
              f"{summ['losses']} losses (win rate {summ['win_rate']}%), "
              f"{summ['skipped']} skipped -> {summ['out_path']}")
        return

    if args.cmd == "train-per-symbol":
        sources = set(args.sources.split(",")) if args.sources else None
        global_data = ds.build(args.path, sources=sources)
        per_sym = ds.build_by_symbol(args.path, sources=sources)
        defaults = tr.current_default_weights()
        results = tr.train_per_symbol(global_data, per_sym, defaults,
                                      l2=args.l2, min_samples=args.min_samples)
        print(f"\n=== PER-STOCK TRAIN (sources={sources or 'all'}) ===")
        n_written = 0
        writer = None
        if args.apply:
            from quant.config import QuantConfig
            writer = QuantConfig(args.out)
        for sym, dirs in results.items():
            print(f"\n#### {sym} ####")
            for d in ("BUY", "SELL"):
                r = dirs[d]
                _print_result(r)
                if writer and r.trained and (r.ship or args.force):
                    for fam in FAMILIES:
                        if fam in r.weights:
                            writer.set_weight(fam, d, float(r.weights[fam]),
                                              symbol=sym)
                            n_written += 1
        if writer:
            print(f"\nwrote {n_written} per-symbol weight(s) into {args.out}")
        else:
            print("\n(dry-run — pass --apply to write per-stock weights)")
        return

    if args.cmd == "backtest":
        from .backtester import compare, format_comparison
        syms = args.symbols.split(",") if args.symbols else None
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        cmp = compare(args.weights, syms, args.start, args.end,
                      cache_dir=args.cache_dir, vix_path=args.vix,
                      modes=modes, stride=args.stride)
        print(format_comparison(cmp))
        # persist a trimmed summary (metrics + per-symbol, no raw trade lists)
        try:
            slim = {"symbols": cmp["symbols"], "start": cmp["start"],
                    "end": cmp["end"], "default": {}, "trained": {}}
            for side in ("default", "trained"):
                for mode, blk in cmp[side]["by_mode"].items():
                    slim[side][mode] = {"overall": blk["overall"],
                                        "by_symbol": blk["by_symbol"],
                                        "trades": blk.get("trades", [])}
            with open(args.save, "w") as f:
                json.dump(slim, f, indent=2)
            print(f"\nsaved summary -> {args.save}")
        except Exception as e:
            print(f"(could not save summary: {e})")
        return

    if args.cmd == "report":
        _print_status(args.path)
        class _A:                       # reuse train with defaults
            path = args.path; sources = None; accepted_only = False
            min_samples = tr.MIN_SAMPLES; l2 = 1.0; floor = tr.WEIGHT_FLOOR
        _run_train(_A())
        return

    if args.cmd == "train":
        _run_train(args)
        return

    if args.cmd == "apply":
        results = _run_train(args)
        if not any(results[d].ship for d in ("BUY", "SELL")):
            print("\nnothing to apply — no direction passed the data guardrails "
                  "AND the out-of-sample baseline gate.")
            return
        if not args.yes:
            ans = input("\nwrite these weights into quant_config.json? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("aborted — nothing written.")
                return
        _apply(results, args.path)


if __name__ == "__main__":
    main()
