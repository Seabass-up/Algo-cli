from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Austin key preparation verifies POSIX file ownership and modes",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_austin_prepare_public_key.py"
SPEC = importlib.util.spec_from_file_location(
    "henry_austin_prepare_public_key_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


KEY = bytes(range(32))
KEY_BASE64URL = base64.urlsafe_b64encode(KEY).decode("ascii").rstrip("=")
KEY_DIGEST = "sha256:" + hashlib.sha256(KEY).hexdigest()


def _environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    tmp_path.chmod(0o700)
    value = {
        "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL": KEY_BASE64URL,
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF_PROTECTED": "true",
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_OS": "macOS",
        "RUNNER_ARCH": "ARM64",
        "RUNNER_TEMP": str(tmp_path.resolve()),
    }
    value.update(changes)
    return value


def test_protected_runner_materializes_exact_private_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(SCRIPT.sys, "platform", "darwin")
    output = tmp_path / SCRIPT.OUTPUT_NAME

    result = SCRIPT.prepare_public_key(
        output=output,
        environment=_environment(tmp_path),
    )

    assert result == {"digest": KEY_DIGEST, "status": "passed"}
    assert output.read_bytes() == KEY
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"GITHUB_ACTIONS": "false"}, "austin_key_runner"),
        ({"GITHUB_EVENT_NAME": "push"}, "austin_key_runner"),
        ({"GITHUB_REF_PROTECTED": "false"}, "austin_key_runner"),
        ({"RUNNER_ENVIRONMENT": "github-hosted"}, "austin_key_runner"),
        ({"RUNNER_ARCH": "X64"}, "austin_key_runner"),
        (
            {"AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL": "bad"},
            "austin_key_encoding",
        ),
    ],
)
def test_runner_and_key_inputs_fail_closed(
    tmp_path: Path,
    monkeypatch,
    changes: dict[str, str],
    reason: str,
) -> None:
    monkeypatch.setattr(SCRIPT.sys, "platform", "darwin")
    with pytest.raises(SCRIPT.AustinKeyPreparationRejected, match=reason):
        SCRIPT.prepare_public_key(
            output=tmp_path / SCRIPT.OUTPUT_NAME,
            environment=_environment(tmp_path, **changes),
        )


def test_noncanonical_base64url_is_rejected() -> None:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(KEY_BASE64URL[-1])
    assert final_index % 4 == 0
    noncanonical = KEY_BASE64URL[:-1] + alphabet[final_index + 1]
    assert len(noncanonical) == 43
    with pytest.raises(SCRIPT.AustinKeyPreparationRejected, match="austin_key_encoding"):
        SCRIPT._decode_key(noncanonical)


def test_output_is_exact_absent_file_under_runner_temp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(SCRIPT.sys, "platform", "darwin")
    environment = _environment(tmp_path)
    outside = tmp_path.parent / SCRIPT.OUTPUT_NAME
    with pytest.raises(SCRIPT.AustinKeyPreparationRejected, match="austin_key_output"):
        SCRIPT.prepare_public_key(output=outside, environment=environment)

    output = tmp_path / SCRIPT.OUTPUT_NAME
    output.write_bytes(b"preserve")
    with pytest.raises(SCRIPT.AustinKeyPreparationRejected, match="austin_key_output"):
        SCRIPT.prepare_public_key(output=output, environment=environment)
    assert output.read_bytes() == b"preserve"


def test_partial_write_is_removed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(SCRIPT.sys, "platform", "darwin")
    output = tmp_path / SCRIPT.OUTPUT_NAME
    real_write = SCRIPT.os.write
    calls = 0

    def interrupted_write(descriptor: int, payload) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, payload[:8])
        raise OSError("interrupted")

    monkeypatch.setattr(SCRIPT.os, "write", interrupted_write)
    with pytest.raises(SCRIPT.AustinKeyPreparationRejected, match="austin_key_write"):
        SCRIPT.prepare_public_key(
            output=output,
            environment=_environment(tmp_path),
        )
    assert not output.exists()


def test_signing_workflow_is_manual_protected_and_nonpublishing() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "henry-austin-signing-qualification.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    trigger = workflow.split("permissions:\n", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "inputs:" not in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "release:" not in trigger
    assert "environment: native-hardening" in workflow
    assert "group: algo-cli-signing" in workflow
    assert "labels: [self-hosted, macOS, ARM64, algo-cli-signing-ephemeral]" in workflow
    protected_guard = workflow.index("if: ${{ github.repository_id != '1297752684' ||")
    checkout = workflow.index("actions/checkout@")
    trust_anchors = (
        "AUSTIN_APPLICATION_IDENTITY",
        "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL",
        "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_SHA256",
        "AUSTIN_EXTENSION_ORIGIN",
        "AUSTIN_INSTALLER_IDENTITY",
        "AUSTIN_NOTARY_PROFILE",
        "AUSTIN_RUNNER_ATTESTATION_SHA256",
        "AUSTIN_TEAM_ID",
    )
    first_secret = min(workflow.index(f"secrets.{name}") for name in trust_anchors)
    assert protected_guard < checkout < first_secret
    runner_preflight = workflow.index("/usr/bin/python3 scripts/henry_austin_signing_runner.py")
    setup_python = workflow.index("actions/setup-python@")
    setup_uv = workflow.index("astral-sh/setup-uv@")
    dependency_install = workflow.index("uv sync --frozen")
    assert checkout < runner_preflight < setup_python < setup_uv < dependency_install
    assert "clean: true" in workflow
    assert "enable-cache: true" not in workflow
    assert "--no-cache" in workflow
    assert "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL" in workflow
    assert "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_SHA256" in workflow
    for trust_anchor in trust_anchors:
        assert f"secrets.{trust_anchor}" in workflow
    assert "inputs." not in workflow
    assert "github.event.inputs" not in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "AUSTIN_BUILD_NUMBER: ${{ github.run_number }}" in workflow
    assert "--version" not in workflow
    assert "--disabled-native-authority-public-key-digest" in workflow
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6" in workflow
    assert "gh-action-pypi-publish" not in workflow
    assert "npm publish" not in workflow
    assert "twine upload" not in workflow
    assert "Remove transient qualification material" in workflow
