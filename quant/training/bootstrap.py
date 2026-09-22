"""quant/training/bootstrap.py — seed the training set from paper_trades.jsonl.

Forward collection starts empty. But the app has ALREADY been paper-trading:
paper_trades.jsonl holds `paper_entry` rows (the full score tree = features) and
`paper_exit` rows (`pnl_per_share` = the outcome). This importer joins them into
labeled training samples so `train`/`report` have real data on day one — no
waiting for fresh collection.

Join: exits are grouped by position_id and their per-share P&Ls summed (an entry
books in partials — target-1 slice + trail/EOD remainder); the total is matched
back to its entry by (symbol, direction, entry_price). label = 1 if total > cost.

These samples are tagged source="bootstrap". They're real but BIASED — only
trades that passed the (untrained) threshold ever entered, so nothing here shows
how rejected candidates would have done. Treat bootstrap data as a sanity check
and a warm start, not a substitute for unbiased forward collection.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict

from .collector import flatten_families, flatten_leaves, DEFAULT_PATH


def _round(x, n=2):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


def import_paper_trades(paper_path: str, out_path: str = DEFAULT_PATH,
                        *, cost: float = 0.0, source: str = "bootstrap") -> dict:
    """Read paper_path, emit labeled samples into out_path (append, deduped).

    Returns a summary dict. Safe to re-run: a (symbol, direction, bar_time,
    source) already present in out_path is skipped."""
    if not os.path.exists(paper_path):
        return {"error": f"not found: {paper_path}"}

    entries: list[dict] = []
    exits_by_pos: dict[str, float] = defaultdict(float)
    exit_meta: dict[str, dict] = {}
    exit_reason: dict[str, str] = {}
    with open(paper_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("type")
            if t == "paper_entry" and r.get("tree"):
                entries.append(r)
            elif t == "paper_exit":
                pid = r.get("position_id")
                if not pid:
                    continue
                exits_by_pos[pid] += float(r.get("pnl_per_share") or 0.0)
                exit_meta[pid] = {"symbol": r.get("symbol"),
                                  "direction": r.get("direction"),
                                  "entry_price": _round(r.get("entry_price"))}
                exit_reason[pid] = r.get("reason") or exit_reason.get(pid, "")

    # index closed positions by (symbol, direction, entry_price) -> [pids]
    closed: dict[tuple, list[str]] = defaultdict(list)
    for pid, meta in exit_meta.items():
        closed[(meta["symbol"], meta["direction"],
                meta["entry_price"])].append(pid)

    # don't re-import rows already present
    seen: set[tuple] = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("source") == source:
                    seen.add((r.get("symbol"), r.get("direction"),
                              r.get("bar_time")))

    written = wins = losses = unmatched = 0
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "a") as out:
        for e in entries:
            sym = e.get("symbol")
            direction = e.get("direction")
            price = _round(e.get("price"))
            pids = closed.get((sym, direction, price))
            if not pids:
                unmatched += 1
                continue
            pid = pids.pop(0)                          # consume one match
            pnl = exits_by_pos.get(pid, 0.0)
            label = 1 if pnl > cost else 0
            trig = e.get("trigger") or {}
            bar_time = (trig.get("bar_time") or e.get("evaluated_at")
                        or e.get("logged_at"))
            key = (sym, direction, bar_time)
            if key in seen:
                continue
            seen.add(key)
            row = {
                "ts": e.get("logged_at"), "bar_time": bar_time,
                "date": str(bar_time)[:10], "symbol": sym,
                "direction": direction, "segment": e.get("segment"),
                "source": source, "price": e.get("price"),
                "composite": _round(e.get("score")),
                "accepted": bool(e.get("accepted")),
                "vetoed": bool(e.get("vetoed_by")),
                "families": flatten_families(e.get("tree")),
                "leaves": flatten_leaves(e.get("tree")),
                "labeled": True, "label": label,
                "outcome_pnl": round(pnl, 6),
                "exit_reason": exit_reason.get(pid), "bars_held": None,
                "label_method": "paper_exit_sum",
            }
            out.write(json.dumps(row, default=str) + "\n")
            written += 1
            wins += label
            losses += (1 - label)

    return {"source": paper_path, "written": written, "wins": wins,
            "losses": losses, "unmatched_entries": unmatched,
            "closed_positions": len(exit_meta)}
