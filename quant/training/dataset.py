"""quant/training/dataset.py — labeled samples -> per-direction (X, y) matrices.

The weights we train are per direction (a family can matter for longs but not
shorts), so BUY and SELL become two independent datasets.

Feature vector = the 5 family scores in canonical FAMILIES order. A family that
was skipped (None) is imputed to the neutral midpoint 50 — "no evidence either
way" — and its presence is tracked in ``availability`` so the trainer can warn
when a family is almost never available (a weight fit on 3 of 500 rows is a lie).

Scores are centered/scaled to [-1, 1] via (score - 50) / 50 for a well-
conditioned logistic fit; the trainer maps coefficients back to raw-score
weights that the live tree (a weighted average over 0-100 scores) can use.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np

from .collector import FAMILIES, DEFAULT_PATH

NEUTRAL = 50.0


@dataclass
class DirData:
    direction: str
    X: np.ndarray                       # (n, 5) scaled features, time-ordered
    y: np.ndarray                       # (n,) win=1
    raw: np.ndarray                     # (n, 5) raw 0-100 scores (imputed)
    dates: np.ndarray = None            # (n,) entry-day strings, for embargo
    availability: dict = field(default_factory=dict)   # family -> frac present
    n: int = 0
    win_rate: float = 0.0


def _load_labeled(path: str, sources: set[str] | None,
                  accepted_only: bool) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if not r.get("labeled") or r.get("label") is None:
                continue
            if sources and r.get("source") not in sources:
                continue
            if accepted_only and not r.get("accepted"):
                continue
            rows.append(r)
    return rows


def build(path: str = DEFAULT_PATH, *, sources: set[str] | None = None,
          accepted_only: bool = False) -> dict[str, DirData]:
    """Return {'BUY': DirData, 'SELL': DirData} from the labeled samples.

    sources        restrict to these collector sources (e.g. {'agent_primary'}
                   for the unbiased firehose); None = all.
    accepted_only  keep only samples that passed the threshold (biased; off by
                   default — the whole point of forward collection is to learn
                   from the rejected ones too)."""
    rows = _load_labeled(path, sources, accepted_only)
    return {d: _build_dir([r for r in rows
                           if str(r.get("direction")).upper() == d], d)
            for d in ("BUY", "SELL")}


def _build_dir(drows: list[dict], direction: str) -> DirData:
    # chronological order so walk-forward folds respect time (never shuffle a
    # time series — see the design doc's validation rules)
    drows = sorted(drows, key=lambda r: str(r.get("bar_time") or r.get("date") or ""))
    raw = np.full((len(drows), len(FAMILIES)), NEUTRAL, dtype=float)
    present = np.zeros(len(FAMILIES), dtype=float)
    y = np.zeros(len(drows), dtype=float)
    dates = np.empty(len(drows), dtype=object)
    for i, r in enumerate(drows):
        fam = r.get("families") or {}
        for j, key in enumerate(FAMILIES):
            v = fam.get(key)
            if v is not None:
                raw[i, j] = float(v)
                present[j] += 1
        y[i] = float(r.get("label") or 0)
        dates[i] = str(r.get("date") or str(r.get("bar_time"))[:10])
    n = len(drows)
    X = (raw - NEUTRAL) / NEUTRAL
    avail = {FAMILIES[j]: (present[j] / n if n else 0.0)
             for j in range(len(FAMILIES))}
    return DirData(direction=direction, X=X, y=y, raw=raw, dates=dates,
                   availability=avail, n=n,
                   win_rate=(float(y.mean()) if n else 0.0))


def build_by_symbol(path: str = DEFAULT_PATH, *, sources: set[str] | None = None,
                    accepted_only: bool = False) -> dict[str, dict]:
    """{symbol: {'BUY': DirData, 'SELL': DirData}} — per-stock datasets."""
    rows = _load_labeled(path, sources, accepted_only)
    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r.get("symbol"), []).append(r)
    out: dict[str, dict] = {}
    for sym, srows in by_sym.items():
        out[sym] = {d: _build_dir([r for r in srows
                                   if str(r.get("direction")).upper() == d], d)
                    for d in ("BUY", "SELL")}
    return out


def status(path: str = DEFAULT_PATH) -> dict:
    """Quick counts for the CLI `status` command — no numpy work."""
    if not os.path.exists(path):
        return {"exists": False, "path": path}
    total = labeled = wins = 0
    by_dir: dict[str, int] = {"BUY": 0, "SELL": 0}
    by_src: dict[str, int] = {}
    dates: set[str] = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            total += 1
            by_dir[str(r.get("direction")).upper()] = \
                by_dir.get(str(r.get("direction")).upper(), 0) + 1
            by_src[r.get("source")] = by_src.get(r.get("source"), 0) + 1
            if r.get("date"):
                dates.add(r["date"])
            if r.get("labeled") and r.get("label") is not None:
                labeled += 1
                wins += int(r["label"])
    return {"exists": True, "path": path, "total": total, "labeled": labeled,
            "pending": total - labeled, "wins": wins,
            "losses": labeled - wins,
            "win_rate": round(100.0 * wins / labeled, 1) if labeled else None,
            "by_direction": by_dir, "by_source": by_src,
            "days": len(dates),
            "date_range": (min(dates), max(dates)) if dates else None}
