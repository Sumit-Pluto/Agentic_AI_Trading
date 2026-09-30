"""Trained-model probability filter (PDF §2.4 learning input).

Wraps state/model_full.pkl (a quant.pipeline MetaModel: LightGBM + isotonic →
calibrated P(win)) as an OPTIONAL filter on agent signals: the agents decide the
BUY/SELL; the model scores each candidate's win-probability and we only take
(or upsize) trades above a threshold. Disabled gracefully if the model / its
deps are absent — the engine then trades on the agents alone.
"""
from .filter import ModelFilter

__all__ = ["ModelFilter"]
