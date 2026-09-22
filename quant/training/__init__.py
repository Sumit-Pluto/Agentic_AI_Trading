"""quant.training — forward-collection training pipeline for the agent weights.

The agent tree (quant/base.py) combines each family's 0-100 score into a
composite via per-direction weights (quant_config.json). Those weights ship as
code defaults (~1.0) — untrained. This package learns them from evidence.

Design (chosen: FORWARD COLLECTION + WIN-PROBABILITY objective):

  1. collector  — a cheap, non-breaking tap on the live sweeps records the
                  family scores of EVERY evaluated (symbol, direction), not just
                  the ones that fired. That kills the selection bias you'd get
                  from training only on trades that passed the untrained bar.
  2. labeler    — later (EOD / next session) each sample gets a forward outcome
                  by walking the rest of that day with the engine's own exit
                  logic (engine.exits.simulate_position): win = realized pnl > 0.
  3. dataset    — labeled samples -> per-direction feature matrix X (the 5
                  family scores) and label vector y (win=1).
  4. trainer    — a pure-numpy L2 logistic regression per direction predicts
                  win probability from the family scores; its coefficients map
                  to non-negative weights (a family that doesn't predict wins
                  gets down-weighted, not negative).
  5. CLI        — `python -m quant.training {status,label,train,apply,report}`
                  writes the trained weights back into quant_config.json — the
                  exact "Phase 2" hand-off the config docstring reserved.

Nothing here runs inside the trading hot path except collector.record(), which
is guarded (try/except, append-only) and can be disabled with
AGENTS_TRAINING_COLLECT=0.
"""

from .collector import TrainingCollector, flatten_families, flatten_leaves

__all__ = ["TrainingCollector", "flatten_families", "flatten_leaves"]
