"""Shared in-memory state for the Gateway backend.

Rule: always access as `state.<name>` — never `from app.core.state import <name>`.
Mutable containers are cleared/updated in place; name-imports would go stale
if a container were ever rebound.
"""
import asyncio

# ---------------------------------------------------------------------------
# Target P&L auto-exit — in-memory state
# ---------------------------------------------------------------------------

# "EXCH|TOKEN" → list of position dicts cached from last broker fetch
_target_positions: dict[str, list[dict]] = {}
# "EXCH|TOKEN" → {enabled, target_value} — per-symbol total-P&L target config
_symbol_targets: dict[str, dict] = {}
# keys ("EXCH|TOKEN") whose FULL position has an auto-exit at the broker this
# session. Shared latch: set and checked by both the symbol and exchange
# target paths so no second trigger re-sells shares an exit order already
# covers. Re-armed when a target is re-enabled or a new order is placed.
_auto_exited_tokens: set[str] = set()
# "EXCH|TOKEN" → last known prices — used for global P&L calculation
_current_ltps: dict[str, float] = {}
_current_bids: dict[str, float] = {}   # best bid (bp1) — exit price for long positions
_current_asks: dict[str, float] = {}   # best ask (sp1) — exit price for short positions
# "EXCH|TOKEN" → previous-day close (the tick's "c" field). Used for the
# per-lot day MTM P&L: (ltp - prev_close) * qty. Only the last-seen close is
# kept; it's constant through the session so a single cached value suffices.
_current_closes: dict[str, float] = {}
# "EXCH|TSYM" → last non-zero unrealized MTM (urmtom) seen while the position
# was open. Frozen so the Positions card can show "MTM at close" once a symbol
# goes flat — the broker reports urmtom=0 for a flat position, so without this
# the number the trader was watching would vanish the instant they square off.
# Only captures closes that happen while the endpoint is being polled; reset
# each trading day (all symbols share one session date) via _last_position_mtm_day.
_last_position_mtm: dict[str, float] = {}
_last_position_mtm_day: str = ""   # today_ist() iso the cache above belongs to
# Per-exchange total P&L targets: exch → {enabled, target_value}. Each exchange
# gets an independent cap because their session timings differ.
_exchange_targets: dict[str, dict] = {}
# exchanges whose auto-exit has already fired this session (re-arm on re-enable)
_exchange_target_exited: set[str] = set()
# "EXCH|TOKEN" → list of per-order (per-lot) target dicts, one per OPEN/PARTIAL
# lot that has its own target armed. Independent of _symbol_targets: each lot
# exits on ITS OWN live P&L (entry → touch), sized to that one lot's open_qty —
# so two batches of the same symbol can carry different targets. Rebuilt from
# the DB by _load_lot_targets (on target set + after every reconcile).
_lot_targets: dict[str, list[dict]] = {}
# lot ids whose per-order target has already placed an auto-exit this session
# (latch; re-armed when the target is re-set or the lot leaves the cache).
_lot_target_exited: set[int] = set()
# Serializes concurrent order-book reconciliation paths
_reconcile_lock = asyncio.Lock()
