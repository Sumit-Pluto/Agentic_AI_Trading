"""quant.pipeline — the v-next intraday pipeline (design of record: docs/INTRADAY_PIPELINE_Vnext.md).

Layered, plug-in, fully testable OFFLINE on synthetic data:

  strategy plugs  ->  feature enrichment  ->  triple-barrier labeler  ->
  meta-model (LightGBM + calibration)  ->  pluggable filter stack  ->
  portfolio selector  ->  (metrics / orders)

Every layer is a small module with a real (if compact) implementation. The
synthetic-data generator (`synthdata.py`) injects a known signal so the training
pipeline can be verified end-to-end without any broker or paid data:

    python -m quant.pipeline selftest      # generate dummy data, train, assert
    python -m quant.pipeline gen           # write dummy data to data/pipeline_dummy/
    python -m quant.pipeline train         # train the meta-model on dummy data
    python -m quant.pipeline infer         # run inference -> ranked candidates
"""

from .contracts import TriggerEvent, Candidate, FilterResult

__all__ = ["TriggerEvent", "Candidate", "FilterResult"]
