"""Trade database & journal (PDF §4 + §2.5) — SQLite, one file, WAL mode.

Everything the engine does leaves a row here: signals, orders (with latency
stamps), positions, trades, intraday equity marks, per-agent stats, brain
registry, job runs. The UI and the reporting engine read ONLY from here.
"""
from .store import Store

__all__ = ["Store"]
