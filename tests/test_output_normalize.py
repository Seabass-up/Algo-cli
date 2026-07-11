"""Tests for H22 — Sequential Output Normalization Pipeline."""
from __future__ import annotations

from algo_cli.intelligence.output_normalize import (
    OutputNormalizationPipeline,
    casual_mode,
    direct_mode,
    hedge_reducer,
)


def test_add_stage() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("hedge", hedge_reducer)
    assert pipe.count() == 1


def test_run_pipeline() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("hedge", hedge_reducer)
    result = pipe.run("I think maybe this works")
    assert "I think" not in result
    assert "maybe" not in result


def test_disable_stage() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("hedge", hedge_reducer)
    pipe.disable("hedge")
    result = pipe.run("I think maybe this works")
    assert "I think" in result


def test_enable_stage() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("hedge", hedge_reducer, enabled=False)
    pipe.enable("hedge")
    result = pipe.run("I think maybe this works")
    assert "I think" not in result


def test_multiple_stages() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("hedge", hedge_reducer)
    pipe.add_stage("direct", direct_mode)
    result = pipe.run("I think could you maybe help")
    assert "could you" not in result.lower()
    assert "maybe" not in result


def test_get_stage_names() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("s1", lambda x: x)
    pipe.add_stage("s2", lambda x: x)
    assert pipe.get_stage_names() == ["s1", "s2"]


def test_get_enabled_stages() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("s1", lambda x: x)
    pipe.add_stage("s2", lambda x: x, enabled=False)
    assert pipe.get_enabled_stages() == ["s1"]


def test_remove_stage() -> None:
    pipe = OutputNormalizationPipeline()
    pipe.add_stage("s1", lambda x: x)
    assert pipe.remove_stage("s1") is True
    assert pipe.count() == 0


def test_remove_missing_stage() -> None:
    pipe = OutputNormalizationPipeline()
    assert pipe.remove_stage("nope") is False


def test_casual_mode() -> None:
    result = casual_mode("Therefore we should go. However, it's late.")
    assert "So" in result
    assert "But" in result


def test_direct_mode() -> None:
    result = direct_mode("Could you help me?")
    assert "Could you" not in result