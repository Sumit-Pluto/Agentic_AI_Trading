"""Intraday exit state machine (I0–I8) — replaces swing_hyena's X-tree.

Priced off the UNDERLYING (liquid) + the option premium (noisy), with a
mandatory session square-off (I0) that overrides everything. See machine.manage.
"""
from .machine import ExitDecision, ExitMarket, manage

__all__ = ["ExitDecision", "ExitMarket", "manage"]
