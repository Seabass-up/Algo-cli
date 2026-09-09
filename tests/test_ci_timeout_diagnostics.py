from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/oliver-ci.yml"


def _test_job(job_name: str) -> tuple[str, dict[str, str]]:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^  {re.escape(job_name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", workflow)
    assert match is not None
    job = match.group(1)
    steps = [step for step in re.split(r"(?m)^      - name: ", job) if "pytest tests" in step]
    assert len(steps) == 1
    # Match this repository's reviewed folded run block, then parse shell words.
    command = steps[0].split("        run: >-\n", 1)[1]
    arguments = shlex.split(command)
    settings = {
        arguments[index + 1].split("=", 1)[0]: arguments[index + 1].split("=", 1)[1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "-o"
    }
    return job, settings


@pytest.mark.parametrize("job_name", ["quality", "test"])
def test_ci_timeout_contract_remains_blocking(job_name: str) -> None:
    job, settings = _test_job(job_name)
    assert settings["faulthandler_timeout"] == "120"
    assert settings.get("faulthandler_exit_on_timeout", "false").lower() == "true"
    assert f"    timeout-minutes: {15 if job_name == 'quality' else 25}\n" in job
    assert "continue-on-error" not in job
    if job_name == "test":
        assert "      fail-fast: false\n" in job


@pytest.mark.parametrize("job_name", ["quality", "test"])
@pytest.mark.parametrize("phase", ["normal", "failure"])
def test_nonstalling_controls_keep_the_configured_watchdog(tmp_path, monkeypatch, job_name, phase) -> None:
    _job, settings = _test_job(job_name)

    def run(command, **kwargs):
        assert f"faulthandler_timeout={settings['faulthandler_timeout']}" in command
        assert "faulthandler_exit_on_timeout=true" in command
        assert kwargs["timeout"] == 20
        return subprocess.CompletedProcess(
            command, 0 if phase == "normal" else 1, "1 passed" if phase == "normal" else "1 failed", ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    test_ci_watchdog_covers_the_whole_test_protocol(tmp_path, job_name, phase)


@pytest.mark.parametrize("job_name", ["quality", "test"])
@pytest.mark.parametrize("phase", ["setup", "call", "teardown", "normal", "failure"])
def test_ci_watchdog_covers_the_whole_test_protocol(tmp_path: Path, job_name: str, phase: str) -> None:
    _job, settings = _test_job(job_name)
    assert float(settings["faulthandler_timeout"]) > 0
    if phase in {"setup", "call", "teardown"}:
        settings["faulthandler_timeout"] = "0.25"
    configuration = tmp_path / "pytest.ini"
    configuration.write_text("[pytest]\n", encoding="utf-8")
    probe = tmp_path / "test_deadline_probe.py"
    probe.write_text(
        "import time\n"
        "import pytest\n"
        f"PHASE = {phase!r}\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    if PHASE == 'setup':\n"
        "        time.sleep(60)\n"
        "    yield\n"
        "    if PHASE == 'teardown':\n"
        "        time.sleep(60)\n"
        "def test_deadline_probe(resource):\n"
        "    if PHASE == 'call':\n"
        "        time.sleep(60)\n"
        "    assert PHASE != 'failure'\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-m", "pytest", "-c", str(configuration), "-p", "no:cacheprovider", "-vv", str(probe)]
    for name, value in settings.items():
        if name.startswith("faulthandler_"):
            command.extend(["-o", f"{name}={value}"])
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
    environment.pop("PYTEST_PLUGINS", None)
    try:
        completed = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("CI test watchdog dumped stacks but did not terminate the stalled worker")
    if phase == "normal":
        assert completed.returncode == 0
        assert "1 passed" in completed.stdout
        assert "Timeout (" not in completed.stderr
    elif phase == "failure":
        assert completed.returncode == 1
        assert "1 failed" in completed.stdout
        assert "Timeout (" not in completed.stderr
    else:
        assert completed.returncode == 1
        assert "test_deadline_probe.py::test_deadline_probe" in completed.stdout
        assert "Timeout (" in completed.stderr
        assert "test_deadline_probe.py" in completed.stderr
