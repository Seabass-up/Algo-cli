from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import conftest


def test_collected_test_ids_fit_the_ci_output_budget(request: pytest.FixtureRequest) -> None:
    oversized = [
        (item.nodeid.partition("[")[0], len(item.nodeid.encode("utf-8")))
        for item in request.session.items
        if len(item.nodeid.encode("utf-8")) > 1024
    ]
    assert oversized == []


@pytest.mark.parametrize(
    "nodeid,valid",
    [
        pytest.param("x" * 1024, True, id="ascii-at-budget"),
        pytest.param("x" * 1025, False, id="ascii-over-budget"),
        pytest.param("\u00e9" * 512, True, id="utf8-at-budget"),
        pytest.param("\u00e9" * 513, False, id="utf8-over-budget"),
    ],
)
def test_collection_guard_counts_encoded_bytes(nodeid: str, valid: bool) -> None:
    items = [SimpleNamespace(nodeid=nodeid)]
    if valid:
        conftest.pytest_collection_modifyitems(items)
    else:
        with pytest.raises(pytest.UsageError, match="short explicit"):
            conftest.pytest_collection_modifyitems(items)


def test_collection_guard_keeps_its_own_error_bounded() -> None:
    canary = "OVERSIZED-FIXTURE-MUST-NOT-ENTER-LOGS"
    items = [SimpleNamespace(nodeid=f"test_case_{index}[{canary * 1000}]") for index in range(100)]
    with pytest.raises(pytest.UsageError) as caught:
        conftest.pytest_collection_modifyitems(items)
    message = str(caught.value)
    assert "100" in message
    assert "test_case_0" in message
    assert "test_case_5" not in message
    assert canary not in message
    assert len(message.encode("utf-8")) < 2048


@pytest.mark.parametrize("explicit_id", [False, True], ids=["reject-unbounded", "accept-labeled"])
def test_real_pytest_bounds_output_without_shrinking_the_fixture(tmp_path: Path, explicit_id: bool) -> None:
    shutil.copyfile(Path(conftest.__file__), tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    ids = ", ids=['oversized-response']" if explicit_id else ""
    (tmp_path / "test_large_fixture.py").write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('payload', [b'x' * (2 * 1024 * 1024 + 1)]{ids})\n"
        "def test_large_fixture(payload):\n"
        "    assert len(payload) == 2 * 1024 * 1024 + 1\n"
        "    assert payload[:1] == b'x'\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
    environment.pop("PYTEST_PLUGINS", None)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(tmp_path / "pytest.ini"), "-p", "no:cacheprovider", "-vv"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert len(output) < 16_384
    if explicit_id:
        assert completed.returncode == 0
        assert b"1 passed" in output
        assert b"test_large_fixture[oversized-response]" in output
    else:
        assert completed.returncode == 4
        assert b"short explicit" in output
