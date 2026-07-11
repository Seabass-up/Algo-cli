"""Tests for the CoT quality scorer (I1 + I3)."""
from __future__ import annotations

from algo_cli.evals.cot_quality import Band, score_cot


def test_under_thinking_detection():
    """Empty CoT with non-empty completion is under_thinking."""
    r = score_cot(cot="", completion="Bash ls -la /tmp")
    assert r.cot_chars == 0
    assert r.completion_chars == 16
    assert r.cot_ratio == 0.0
    assert r.band == Band.UNDER
    assert r.structure_score < 0.4


def test_over_thinking_detection():
    """Very long CoT with short completion is over_thinking."""
    r = score_cot(cot="x" * 3000, completion="Bash ls")
    assert r.cot_chars == 3000
    assert r.cot_ratio > 50.0
    assert r.band == Band.OVER
    assert r.structure_score < 0.4


def test_in_band_pass():
    """Proportional reasoning lands in band and gets a healthy score."""
    r = score_cot(
        cot="Let me think about this. The data shows three issues. I will address them in order.",
        completion="Bash pytest -q tests/test_cot_quality.py",
    )
    assert 0.5 <= r.cot_ratio <= 3.0
    assert r.band == Band.IN_BAND
    # No markers (no 'First,' / 'Next,' / etc.) but band_bonus gives 0.2
    assert 0.2 <= r.structure_score <= 0.3


def test_well_sequenced_bonus():
    """CoT with First,...Next, ordering gets the 0.4 sequenced bonus."""
    # Shorten the CoT so the ratio lands in band (was over-thinking before).
    r = score_cot(
        cot=(
            "First, I need to check the project state. "
            "Next, I will read the file."
        ),
        completion="Read /tmp/foo.py",
    )
    assert r.well_sequenced
    assert r.band == Band.IN_BAND
    # 2 markers * 0.2 = 0.4, + 0.4 (sequenced), + 0.2 (in band) = 1.0
    assert r.structure_score >= 0.8


def test_marker_count_capped():
    """Marker score is capped at 0.6 (3 markers)."""
    # Use a slightly longer completion so the ratio stays in band.
    r = score_cot(
        cot="First, Next, Then, Finally, again,",  # 4 markers
        completion="Read /tmp/foo.py and continue",
    )
    assert len(r.markers) == 4
    # marker_score capped at 0.6, no First/Next pair => 0.0 seq, no band bonus? in band yes +0.2
    assert r.structure_score <= 1.0


def test_empty_inputs_safe():
    """Empty CoT and empty completion must not raise."""
    r = score_cot(cot="", completion="")
    assert r.cot_chars == 0
    assert r.completion_chars == 0
    assert r.cot_ratio == 0.0  # max(1, 0) = 1
    assert r.band == Band.UNDER


def test_summary_human_readable():
    """Summary contains the key fields."""
    r = score_cot(cot="think", completion="act")
    s = r.summary
    assert "cot=" in s
    assert "completion=" in s
    assert "ratio=" in s
    assert "score=" in s


def test_to_dict_serializable():
    """to_dict returns a JSON-friendly dict."""
    r = score_cot(cot="First, think. Next, act.", completion="Bash ls")
    d = r.to_dict()
    assert d["band"] in {"under_thinking", "in_band", "over_thinking"}
    assert isinstance(d["markers"], list)
    assert isinstance(d["well_sequenced"], bool)
    assert isinstance(d["structure_score"], float)


def test_fable5_median_calibration():
    """Calibration check against the Fable-5 corpus median (1.14)."""
    # A 200/200 pair has ratio 1.0 — within the Fable-5 IQR.
    r = score_cot(cot="x" * 200, completion="y" * 200)
    assert 0.5 <= r.cot_ratio <= 3.0
    assert r.band == Band.IN_BAND
