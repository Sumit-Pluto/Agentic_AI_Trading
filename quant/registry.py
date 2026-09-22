"""Tree assembly: the single place where the quant hierarchy is defined.

Adding a future agent family = write quant/agents/<family>.py with a
build() -> QuantAgent, import it here, append to children. Nothing else
changes — config, UI drill-down, weights and toggles pick it up
automatically from the tree walk.
"""

from __future__ import annotations

from .base import QuantAgent


class RootQuant(QuantAgent):
    key = "quant"
    name = "Q — Quant Score"
    description = "Composite of all agent families; compared to the signal threshold"


def build_root() -> QuantAgent:
    from .agents.macro import build as build_macro
    from .agents.smc import build as build_smc
    from .agents.snr import build as build_snr
    from .agents.volatility import build as build_volatility
    from .agents.volume import build as build_volume

    return RootQuant(children=[
        build_smc(),
        build_snr(),
        build_volatility(),
        build_volume(),
        build_macro(),      # real macro/news family (quant/agents/macro.py)
    ])
