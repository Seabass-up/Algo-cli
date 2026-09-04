from __future__ import annotations

import base64
import hashlib
import importlib.util
from importlib.metadata import FileHash, PackagePath
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_echo_veil_dependency_audit.py"
SPEC = importlib.util.spec_from_file_location("henry_echo_dependency_audit", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


def _direct_url(*, commit: str = SCRIPT.EXPECTED_COMMIT) -> str:
    return json.dumps(
        {
            "url": SCRIPT.EXPECTED_REPOSITORY,
            "vcs_info": {
                "vcs": "git",
                "commit_id": commit,
                "requested_revision": commit,
            },
        }
    )


def test_repository_and_installed_echo_dependency_pass() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    workflow = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")

    assert report["passed"] is True
    assert report["version"] == "0.8.0"
    assert report["commit"] == SCRIPT.EXPECTED_COMMIT
    assert report["verified_python_files"] > 0
    assert report["verified_module_origins"] == 2
    assert report["source_tree_sha256"] == SCRIPT.QUALIFIED_ECHO_SOURCE_TREE_SHA256
    assert "--no-emit-package echo-veil" in workflow
    assert "--require-hashes --disable-pip --strict" in workflow
    assert "python -I scripts/henry_echo_veil_dependency_audit.py" in workflow


def test_hosted_echo_install_and_audit_paths_require_copy_link_mode() -> None:
    ci = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")

    required_ci_commands = (
        "uv sync --frozen --no-editable --extra dev --extra supply-chain --extra echo-veil "
        "--reinstall-package algo-cli-runtime --link-mode copy",
        "uv run --frozen --no-editable --extra dev --extra supply-chain "
        "--extra echo-veil --link-mode copy python -I scripts/henry_echo_veil_dependency_audit.py",
        "uv run --frozen --no-editable --extra dev --extra supply-chain --extra echo-veil "
        "--link-mode copy pytest tests",
        "uv sync --frozen --no-editable --extra dev --extra echo-veil "
        "--reinstall-package algo-cli-runtime --link-mode copy",
        "uv run --frozen --no-editable --extra dev --extra echo-veil --link-mode copy pytest tests",
    )
    required_release_commands = (
        "uv sync --frozen --no-editable --extra dev --extra release --extra supply-chain "
        "--extra echo-veil --reinstall-package algo-cli-runtime --link-mode copy",
        "uv run --frozen --no-editable --extra dev --extra release --extra supply-chain "
        "--extra echo-veil --link-mode copy python -I scripts/henry_echo_veil_dependency_audit.py",
        "uv run --frozen --no-editable --extra dev --extra release --extra supply-chain "
        "--extra echo-veil --link-mode copy pytest tests",
    )

    normalized_ci = " ".join(ci.split())
    normalized_release = " ".join(release.split())
    assert all(command in normalized_ci for command in required_ci_commands)
    assert all(command in normalized_release for command in required_release_commands)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (None, "direct_url_invalid"),
        ("{}", "direct_url_schema"),
        (_direct_url(commit="f" * 40), "direct_url_commit"),
        (
            json.dumps(
                {
                    "url": SCRIPT.EXPECTED_REPOSITORY,
                    "vcs_info": {
                        "vcs": "git",
                        "commit_id": SCRIPT.EXPECTED_COMMIT,
                        "requested_revision": SCRIPT.EXPECTED_COMMIT,
                    },
                    "editable": True,
                }
            ),
            "direct_url_schema",
        ),
    ],
)
def test_direct_url_identity_fails_closed(document: str | None, reason: str) -> None:
    with pytest.raises(SCRIPT.EchoDependencyAuditError, match=reason):
        SCRIPT._validate_direct_url(document)


def test_record_tamper_is_rejected(tmp_path: Path) -> None:
    installed = tmp_path / "site"
    package = installed / "echo_veil"
    package.mkdir(parents=True)
    targets = {
        "echo_veil/__init__.py": package / "__init__.py",
        "echo_veil/agent_memory.py": package / "agent_memory.py",
    }
    members: list[PackagePath] = []
    for relative, target in targets.items():
        target.write_text("VALUE = 1\n", encoding="utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(target.read_bytes()).digest()).rstrip(b"=").decode("ascii")
        member = PackagePath(relative)
        member.hash = FileHash(f"sha256={digest}")
        member.size = target.stat().st_size
        members.append(member)

    class Distribution:
        version = SCRIPT.EXPECTED_VERSION
        files = members

        @staticmethod
        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return _direct_url()

        @staticmethod
        def locate_file(path: PackagePath) -> Path:
            return installed / path

    targets["echo_veil/agent_memory.py"].write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(SCRIPT.EchoDependencyAuditError, match="record_hash_mismatch"):
        SCRIPT._verify_distribution(
            Distribution(),
            install_prefix=installed,
            module_version=SCRIPT.EXPECTED_VERSION,
            module_origins={
                "echo_veil": str(targets["echo_veil/__init__.py"]),
                "echo_veil.agent_memory": str(targets["echo_veil/agent_memory.py"]),
            },
        )


def test_shadow_package_cannot_borrow_trusted_distribution_identity(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow" / "echo_veil"
    shadow.mkdir(parents=True)
    poison = tmp_path / "shadow-executed"
    (shadow / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(poison)!r}).write_text('poison', encoding='utf-8')\n"
        f'__version__ = "{SCRIPT.EXPECTED_VERSION}"\n',
        encoding="utf-8",
    )
    (shadow / "agent_memory.py").write_text("AgentMemory = object\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(shadow.parent)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert report["audit"] == "henry-echo-veil-dependency-v1"
    assert report["passed"] is True
    assert report["distribution"] == SCRIPT.EXPECTED_NAME
    assert report["version"] == SCRIPT.EXPECTED_VERSION
    assert report["commit"] == SCRIPT.EXPECTED_COMMIT
    assert report["source_tree_sha256"] == SCRIPT.QUALIFIED_ECHO_SOURCE_TREE_SHA256
    assert poison.exists() is False


def test_operator_entrypoint_is_content_free_and_works_outside_checkout(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-I", str(SCRIPT_PATH)],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"] is True
    assert str(ROOT) not in completed.stdout
