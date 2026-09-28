"""AI Market Intelligence (PDF §2.4) — the intraday agent tree.

24 deterministic pure-function agents across six families:
  R  Regime      (index-level, run once/tick; incl. R6 macro event gate)
  S  Setup       (price structure: ORB, VWAP, first-hour, PDH/PDL, gap)
  M  Momentum    (RSI2, Raschke 3/10, volume shock, range expansion)
  F  Flow / OI   (buildup, PCR mechanism, walls/max-pain, OTM footprint, O:S)
  V  Volatility  (VRP, 25-delta skew, ATM straddle, term structure)
  C  Catalyst    (per-name event gate)

Importing this package registers every agent into base.REGISTRY. The scanner
runs R (+R6) once on the index, then S/F/V/M/C per candidate, and the combiner
fuses them exactly as swing_hyena does.
"""
from . import catalyst, flow, momentum, regime, setup, volatility  # noqa: F401 (register)
from .base import (REGISTRY, AgentInput, AgentResult, chain_features, clip01,
                   run_all, run_family, z_to_score)

__all__ = ["REGISTRY", "AgentInput", "AgentResult", "run_all", "run_family",
           "chain_features", "clip01", "z_to_score"]
