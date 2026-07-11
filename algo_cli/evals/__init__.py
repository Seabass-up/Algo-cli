"""algo_cli/evals — Evaluation, scoring, and quality utilities.

This package exposes reusable evaluators that can be invoked by:
- the agent runtime to gate tool calls
- the eval suite to grade trajectories
- the policy chain to gather quality evidence

Modules:
- cot_quality: I1 + I3 — CoT proportionality and sequencing-marker scoring
"""
from .cot_quality import Band, CoTQuality, score_cot
from .performance_regression import CUSUMResult, RegressionState, detect_cusum

__all__ = [
    "Band",
    "CUSUMResult",
    "CoTQuality",
    "RegressionState",
    "detect_cusum",
    "score_cot",
]
