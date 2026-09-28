"""Options analytics — local Black-Scholes greeks/IV + chain models.

Shoonya sends neither IV nor greeks, so they are computed here. Ported from
OptionSmith (mathx.py verbatim; models.py trimmed to OptionQuote/Chain).
"""
from .chain_builder import (build_chain, calibrate_carry, fill_missing_ivs,
                            from_gateway_payload, greeks_for)
from .mathx import (RISK_FREE, bs_delta, bs_gamma, bs_price, bs_theta, bs_vega,
                    implied_vol)
from .models import (Chain, OptionQuote, ist_now, ist_today, market_session)

__all__ = [
    "Chain", "OptionQuote", "build_chain", "from_gateway_payload",
    "fill_missing_ivs", "calibrate_carry", "greeks_for",
    "bs_price", "bs_delta", "bs_gamma", "bs_theta", "bs_vega", "implied_vol",
    "RISK_FREE", "ist_now", "ist_today", "market_session",
]
