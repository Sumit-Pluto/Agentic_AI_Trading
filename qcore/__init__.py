"""qcore — C++ compute kernel (Black-Scholes / implied vol), Python fallback.

Prefers the compiled extension ``qcore._qcore`` (build it with
``qcore/build.sh``). If it isn't built, transparently falls back to the pure
Python reference in :mod:`quant.mathutils`, so the app always runs — just
slower. Import everywhere as::

    from qcore import bs_price, bs_gamma, implied_vol, implied_vols

``qcore.BACKEND`` is ``"cpp"`` or ``"python"`` so callers/logs can report which
path is live. See docs/CPP_ENGINE_AND_FEED_ARCHITECTURE.md (Phase 2).
"""
from __future__ import annotations

try:
    from qcore._qcore import (  # type: ignore
        RISK_FREE, bs_gamma, bs_price, bs_vega, implied_vol, implied_vols,
        simulate_zones, years_to_expiry,
    )
    BACKEND = "cpp"
except ImportError:                       # extension not compiled — use Python
    from quant.mathutils import (  # noqa: F401
        RISK_FREE, bs_gamma, bs_price, bs_vega, implied_vol, years_to_expiry,
    )
    BACKEND = "python"

    def implied_vols(is_call, price, spot, strike, t_years, r=RISK_FREE):
        """Vectorised IV fallback: NaN where no solution (mirrors the C++)."""
        import numpy as np
        out = np.empty(len(price), dtype=float)
        for i in range(len(price)):
            v = implied_vol(is_call, price[i], spot[i], strike[i], t_years, r)
            out[i] = float("nan") if v is None else v
        return out


__all__ = ["bs_price", "bs_gamma", "bs_vega", "implied_vol", "implied_vols",
           "years_to_expiry", "RISK_FREE", "BACKEND"]
