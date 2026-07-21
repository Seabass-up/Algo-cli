from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli.david_control_kernel import MAX_FRAME_BYTES


ROOT = Path(__file__).resolve().parents[1]
FUZZER_PATH = ROOT / "scripts" / "david_control_kernel_fuzzer.py"
FUZZER_SPEC = importlib.util.spec_from_file_location("david_control_kernel_fuzzer", FUZZER_PATH)
assert FUZZER_SPEC is not None and FUZZER_SPEC.loader is not None
FUZZER = importlib.util.module_from_spec(FUZZER_SPEC)
sys.modules[FUZZER_SPEC.name] = FUZZER
FUZZER_SPEC.loader.exec_module(FUZZER)

FUZZ_SEED = FUZZER.FUZZ_SEED
DavidFuzzReport = FUZZER.DavidFuzzReport
fuzz_control_frames = FUZZER.fuzz_control_frames
main = FUZZER.main


def test_bounded_fuzzer_is_deterministic_and_rejects_every_case() -> None:
    first = fuzz_control_frames(iterations=500, seed=FUZZ_SEED)
    second = fuzz_control_frames(iterations=500, seed=FUZZ_SEED)

    assert first == second
    assert first.passed is True
    assert first.rejected == 500
    assert first.unexpected_accepts == 0
    assert first.unexpected_crashes == 0
    assert first.maximum_case_bytes <= MAX_FRAME_BYTES + 8
    assert 0 < first.maximum_buffered_bytes <= MAX_FRAME_BYTES
    assert len(first.mode_counts) == 25
    assert all(count > 0 for count in first.mode_counts.values())
    assert first.corpus_digest.startswith("sha256:")
    assert first.classification_digest.startswith("sha256:")


def test_different_seed_changes_mutation_classification_digest() -> None:
    first = fuzz_control_frames(iterations=250, seed=1)
    second = fuzz_control_frames(iterations=250, seed=2)
    assert first.passed and second.passed
    assert first.corpus_digest == second.corpus_digest
    assert first.classification_digest != second.classification_digest


@pytest.mark.parametrize("iterations", [0, 24, 1_000_001, True])
def test_iteration_bounds_reject(iterations) -> None:
    with pytest.raises(ValueError, match="iterations"):
        fuzz_control_frames(iterations=iterations)


@pytest.mark.parametrize("seed", [-1, 1 << 63, True])
def test_seed_bounds_reject(seed) -> None:
    with pytest.raises(ValueError, match="seed"):
        fuzz_control_frames(iterations=25, seed=seed)


def test_report_pass_requires_zero_accepts_and_crashes() -> None:
    report = fuzz_control_frames(iterations=25)
    assert replace(report, unexpected_accepts=1).passed is False
    assert replace(report, unexpected_crashes=1).passed is False
    assert replace(report, rejected=24).passed is False
    assert (
        DavidFuzzReport(
            iterations=25,
            seed=FUZZ_SEED,
            rejected=25,
            unexpected_accepts=0,
            unexpected_crashes=0,
            maximum_case_bytes=1,
            maximum_buffered_bytes=1,
            corpus_digest="sha256:" + "0" * 64,
            classification_digest="sha256:" + "0" * 64,
            mode_counts={},
        ).passed
        is False
    )


def test_cli_emits_machine_readable_evidence(capsys) -> None:
    assert main(["--iterations", "50", "--seed", "7"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is True
    assert output["iterations"] == 50
    assert output["unexpected_accepts"] == 0
    assert output["unexpected_crashes"] == 0
    assert output["schema_version"] == 1


def test_operator_fuzzer_runs_from_outside_checkout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(FUZZER_PATH), "--iterations", "50", "--seed", "7"],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["passed"] is True
    assert output["iterations"] == 50
