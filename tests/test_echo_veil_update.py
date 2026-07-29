"""Regression coverage for deterministic, source-qualified Echo maintenance."""

from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from algo_cli import echo_veil_update


class FakeDistribution:
    def __init__(
        self,
        *,
        version: str = echo_veil_update.QUALIFIED_ECHO_VEIL_VERSION,
        commit: str = echo_veil_update.QUALIFIED_ECHO_VEIL_COMMIT,
        url: str = echo_veil_update.ECHO_VEIL_REPOSITORY_GIT,
    ) -> None:
        self.version = version
        self.commit = commit
        self.url = url

    def read_text(self, name: str) -> str:
        assert name == "direct_url.json"
        return json.dumps(
            {
                "url": self.url,
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": self.commit,
                    "requested_revision": self.commit,
                },
            }
        )


def _module_importer(name: str) -> object:
    if name == "echo_veil":
        return SimpleNamespace(
            __version__=echo_veil_update.QUALIFIED_ECHO_VEIL_VERSION
        )
    if name == "echo_veil.agent_memory":
        return SimpleNamespace(
            AgentMemory=object(),
            AlwaysAvailableMemory=object(),
            EmbeddingUnavailable=object(),
            HashingTextEmbedder=object(),
            OllamaTextEmbedder=object(),
        )
    raise ImportError(name)


def _readiness(_config: object) -> dict[str, object]:
    return {
        "version_supported": True,
        "enabled": True,
        "protection_policy": "required",
        "healthy": True,
        "local_protection_ready": True,
    }


def _status(**overrides: object) -> echo_veil_update.EchoStatus:
    values: dict[str, object] = {
        "installed": True,
        "installed_version": "0.6.0",
        "source_version": "0.6.0",
        "installation_kind": "vcs-pinned",
        "source_url": echo_veil_update.ECHO_VEIL_REPOSITORY_GIT,
        "source_commit": echo_veil_update.PREVIOUS_QUALIFIED_COMMITS["0.6.0"],
        "api_contract_ready": True,
        "adapter_supported": False,
        "enabled": True,
        "protection_policy": "required",
        "healthy": False,
        "local_protection_ready": False,
        "qualified": False,
        "upstream_version": None,
        "upstream_error": None,
    }
    values.update(overrides)
    return echo_veil_update.EchoStatus(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("do a review of echo-veil to see if it is updated", "status"),
        ("is Echo Veil up to date?", "status"),
        ("echo status", "status"),
        ("update echo", "update"),
        ("please update echo veil now", "update"),
        ("echo-veil repair", "update"),
        ("explain how echoes work", None),
    ],
)
def test_transcript_echo_intent_is_deterministic(text: str, expected: str | None) -> None:
    assert echo_veil_update.classify_echo_request(text) == expected


def test_semantic_version_ordering_distinguishes_minor_releases() -> None:
    assert echo_veil_update._version_tuple("0.6.0") == (0, 6, 0)
    assert echo_veil_update._version_tuple("v0.7.0") == (0, 7, 0)
    assert echo_veil_update._version_tuple("not-a-version") is None
    assert max(
        ("0.6.0", "0.7.0"),
        key=lambda value: echo_veil_update._version_tuple(value) or (0, 0, 0),
    ) == "0.7.0"


def test_status_separates_qualified_install_from_unverified_upstream() -> None:
    status = echo_veil_update.collect_echo_status(
        object(),
        distribution_getter=lambda _name: FakeDistribution(),
        module_importer=_module_importer,
        readiness_getter=_readiness,
        upstream_fetcher=lambda: (None, "unavailable"),
    )
    rendered = echo_veil_update.render_echo_status(status)

    assert status.qualified is True
    assert status.upstream_current is None
    assert "Algo qualified: yes" in rendered
    assert "Upstream latest: unknown (unavailable)" in rendered
    assert "Installed equals verified upstream latest: unknown" in rendered
    assert "up to date" not in rendered.casefold()


def test_status_rejects_wrong_source_revision_even_when_versions_match() -> None:
    status = echo_veil_update.collect_echo_status(
        object(),
        include_upstream=False,
        distribution_getter=lambda _name: FakeDistribution(commit="a" * 40),
        module_importer=_module_importer,
        readiness_getter=_readiness,
    )

    assert status.adapter_supported is True
    assert status.qualified is False
    assert status.exit_code == 1


def test_missing_distribution_is_reported_without_inventing_a_version() -> None:
    def missing(_name: str) -> object:
        raise metadata.PackageNotFoundError

    status = echo_veil_update.collect_echo_status(
        object(),
        include_upstream=False,
        distribution_getter=missing,
        module_importer=lambda _name: (_ for _ in ()).throw(ImportError()),
        readiness_getter=lambda _config: {},
    )

    assert status.installed is False
    assert status.installed_version is None
    assert status.source_version is None
    assert status.qualified is False


def test_upstream_lookup_uses_only_the_canonical_repository() -> None:
    requested: list[str] = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return self.payload

    def opener(request, *, timeout):
        assert timeout == echo_veil_update.UPSTREAM_TIMEOUT_SECONDS
        requested.append(request.full_url)
        if request.full_url.endswith("/releases/latest"):
            return Response(b'{"tag_name":"v0.6.0"}')
        return Response(b'[{"name":"v0.7.0"}]')

    version, error = echo_veil_update.fetch_upstream_latest(opener=opener)

    assert version == "0.7.0"
    assert error is None
    assert requested == [
        "https://api.github.com/repos/Seabass-up/echo-veil/releases/latest",
        "https://api.github.com/repos/Seabass-up/echo-veil/tags?per_page=1",
    ]


def test_real_lifecycle_preflight_runs_against_qualified_sibling_source() -> None:
    configured_source = os.environ.get("ALGO_TEST_ECHO_SOURCE")
    echo_source = (
        Path(configured_source)
        if configured_source
        else Path(__file__).resolve().parents[2] / "echo-veil" / "src"
    )
    if not (echo_source / "echo_veil" / "agent_memory.py").is_file():
        pytest.skip("qualified sibling Echo Veil source is unavailable")

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            echo_veil_update._LIFECYCLE_VERIFY_SCRIPT,
            str(echo_source),
            echo_veil_update.QUALIFIED_ECHO_VEIL_VERSION,
            echo_veil_update.QUALIFIED_ECHO_VEIL_COMMIT,
            "0",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=echo_veil_update.VERIFY_TIMEOUT_SECONDS,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["lifecycle"] is True


def test_update_does_not_mutate_an_already_qualified_install() -> None:
    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(
            installed_version="0.7.0",
            source_version="0.7.0",
            source_commit=echo_veil_update.QUALIFIED_ECHO_VEIL_COMMIT,
            qualified=True,
            adapter_supported=True,
            healthy=True,
            local_protection_ready=True,
        ),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runner must not be called")
        ),
    )

    assert result.returncode == 0
    assert result.changed is False
    assert "No package files were changed" in result.message


def test_update_refuses_unreviewed_version_before_mutation() -> None:
    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(installed_version="9.9.9"),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runner must not be called")
        ),
    )

    assert result.returncode == 65
    assert result.changed is False
    assert "refused to mutate" in result.message


def _build_wheel_for(command: list[str]) -> None:
    wheel_dir = Path(command[command.index("--wheel-dir") + 1])
    wheel_dir.joinpath("echo_veil-0.7.0-py3-none-any.whl").write_bytes(b"wheel")


def test_update_stages_before_install_and_uses_no_shell() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if "wheel" in command:
            _build_wheel_for(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(),
        executable="/runtime/python",
        runner=runner,
        installer="pip",
    )

    assert result.returncode == 0
    assert result.changed is True
    assert [("wheel" in call, "--target" in call, "--force-reinstall" in call, "-I" in call) for call, _ in calls] == [
        (True, False, False, False),
        (False, True, False, False),
        (False, False, False, True),
        (False, False, True, False),
        (False, False, False, True),
    ]
    assert echo_veil_update.QUALIFIED_ECHO_VEIL_REQUIREMENT in calls[0][0]
    assert echo_veil_update.QUALIFIED_ECHO_VEIL_REQUIREMENT in calls[3][0]
    assert all("shell" not in kwargs for _command, kwargs in calls)


def test_failed_staging_preflight_never_mutates_runtime() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "wheel" in command:
            _build_wheel_for(command)
        returncode = 7 if "-I" in command else 0
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout="",
            stderr="preflight failed" if returncode else "",
        )

    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(),
        runner=runner,
        installer="pip",
    )

    assert result.returncode == 7
    assert "runtime was not changed" in result.message
    assert not any("--force-reinstall" in command for command in calls)


def test_failed_fresh_process_verification_rolls_back_previous_revision() -> None:
    calls: list[list[str]] = []
    verifier_count = 0

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal verifier_count
        calls.append(command)
        if "wheel" in command:
            _build_wheel_for(command)
        if "-I" in command:
            verifier_count += 1
            returncode = 9 if verifier_count == 2 else 0
        else:
            returncode = 0
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout="ok" if returncode == 0 else "",
            stderr="verification failed" if returncode else "",
        )

    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(),
        executable="/runtime/python",
        runner=runner,
        installer="pip",
    )

    previous_commit = echo_veil_update.PREVIOUS_QUALIFIED_COMMITS["0.6.0"]
    assert result.returncode == 1
    assert result.rollback_attempted is True
    assert result.rollback_succeeded is True
    assert result.after_version == "0.6.0"
    assert any(
        any(previous_commit in argument for argument in command)
        for command in calls
    )


def test_uv_owned_runtime_stages_and_installs_without_python_pip() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = echo_veil_update.update_echo_veil(
        object(),
        before=_status(),
        executable="/uv-runtime/python",
        runner=runner,
        installer="uv",
        which=lambda name: "/usr/local/bin/uv" if name == "uv" else None,
    )

    assert result.returncode == 0
    assert len(calls) == 4
    assert calls[0][0][:3] == ["/usr/local/bin/uv", "pip", "install"]
    assert "--target" in calls[0][0]
    assert calls[1][0][1:3] == ["-I", "-c"]
    assert calls[2][0][:3] == ["/usr/local/bin/uv", "pip", "install"]
    assert "--reinstall-package" in calls[2][0]
    assert calls[3][0][1:3] == ["-I", "-c"]
    assert all("shell" not in kwargs for _command, kwargs in calls)
