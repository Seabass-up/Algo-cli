from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "david_hardening_gate.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("david_hardening_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_hardening_change_set_is_authorized_and_named() -> None:
    gate = _load_gate()
    assert gate.run_gate() == []


def test_unqualified_browser_and_computer_surfaces_are_absent() -> None:
    gate = _load_gate()
    assert gate.validate_unqualified_capability_exposure() == []


def test_unqualified_interactive_names_fail_closed_without_false_positives() -> None:
    gate = _load_gate()
    blocked = {
        "browser.open",
        "browser_snapshot",
        "/browser",
        "computer.click",
        "desktop_type",
        "chrome.navigate",
        "austin.desktop.scroll",
        "ui.submit",
        "mouse.move",
        "keyboard.press",
        "dom.fill",
    }
    allowed = {
        "web_fetch",
        "web_search",
        "vision.screenshot_verify",
        "screenshot_description_verify",
        "/vision",
        "/config",
    }
    assert all(gate._interactive_capability_name(name) for name in blocked)
    assert all(not gate._interactive_capability_name(name) for name in allowed)


def test_unqualified_surface_import_and_activation_are_rejected(tmp_path: Path) -> None:
    gate = _load_gate()
    activation = tmp_path / "AustinNativeControlActivation.json"
    activation.write_text("{}", encoding="utf-8")
    errors = gate.validate_unqualified_capability_exposure(
        registries={
            "tool": {"browser.open", "read_file"},
            "action": {"computer.click"},
            "kernel": {"vision.screenshot_verify"},
            "slash": {"/browser", "/help"},
        },
        runtime_imports={
            "algo_cli/tools.py": {"algo_cli.boron_browser_entry"},
        },
        activation_paths=(activation,),
    )
    assert errors == [
        "action exposes unqualified interactive capability 'computer.click' during freeze",
        "slash exposes unqualified interactive capability '/browser' during freeze",
        "tool exposes unqualified interactive capability 'browser.open' during freeze",
        "algo_cli/tools.py imports disabled capability module "
        "'algo_cli.boron_browser_entry' during freeze",
        f"{activation}: production computer-control activation is forbidden during freeze",
    ]


def test_registry_parser_accepts_utf8_bom_and_rejects_dynamic_entries(
    tmp_path: Path,
) -> None:
    gate = _load_gate()
    registry = tmp_path / "david_tools.py"
    registry.write_bytes(b"\xef\xbb\xbfALL_TOOLS = [read_file]\n")
    assert gate._tool_names(registry) == {"read_file"}

    registry.write_text("ALL_TOOLS = [*dynamic_tools]\n", encoding="utf-8")
    with pytest.raises(gate.GateError, match="non-static or unrecognized"):
        gate._tool_names(registry)


def test_undeclared_path_is_rejected() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    ledger = gate._load_json(gate.LEDGER_PATH)
    errors = gate.validate_changed_paths(freeze, ledger, changed_paths={"algo_cli/new_feature.py"})
    assert errors == ["algo_cli/new_feature.py: changed during freeze without ledger authorization"]


def test_naming_categories_fail_closed() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    cases = {
        "algo_cli/process_runtime.py": "process",
        "algo_cli/memory_store.py": "memory",
        "algo_cli/chrome_bridge.py": "browser",
        "algo_cli/macos_accessibility.py": "computer",
    }
    for path, category in cases.items():
        error = gate.validate_filename(path, freeze)
        assert error is not None
        assert f": {category} file lacks" in error


def test_naming_categories_accept_required_tokens() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    for path in (
        "algo_cli/marcus_process_runtime.py",
        "algo_cli/ada_memory_store.py",
        "algo_cli/carbon_chrome_bridge.py",
        "algo_cli/tokyo_macos_accessibility.py",
    ):
        assert gate.validate_filename(path, freeze) is None


def test_naming_gate_tokenizes_camel_case_and_acronyms() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    assert gate.validate_filename("native/austin/AustinXPCProtocol.swift", freeze) is None
    assert gate.validate_filename("native/austin/AustinTCCAdapter.swift", freeze) is None
    assert gate.validate_filename("native/austin/BostonScreenCapture.swift", freeze) is None
    assert gate.validate_filename("native/austin/UnsafeXPCProtocol.swift", freeze) is not None


def test_verified_requirement_requires_evidence() -> None:
    gate = _load_gate()
    ledger = gate._load_json(gate.LEDGER_PATH)
    ledger["requirements"][0] = {**ledger["requirements"][0], "status": "verified", "evidence": []}
    errors = gate.validate_ledger(ledger)
    assert "HARD-001: verified without evidence" in errors


def test_verified_milestone_requires_evidence() -> None:
    gate = _load_gate()
    ledger = gate._load_json(gate.LEDGER_PATH)
    ledger["milestones"][0] = {**ledger["milestones"][0], "status": "verified", "evidence": []}
    errors = gate.validate_ledger(ledger)
    assert "M0: verified milestone has no evidence" in errors


def test_ledger_rejects_noncanonical_evidence_and_status_contradictions() -> None:
    gate = _load_gate()
    ledger = gate._load_json(gate.LEDGER_PATH)
    first = dict(ledger["requirements"][0])
    evidence = dict(first["evidence"][0])
    evidence["digest"] = "sha256:not-canonical"
    evidence["timestamp"] = "2026-02-30T00:00:00Z"
    evidence["extra"] = "smuggled"
    first["evidence"] = [evidence]
    ledger["requirements"][0] = first
    errors = gate.validate_ledger(ledger)
    assert "HARD-001: evidence[0] must use the exact evidence schema" in errors

    ledger = gate._load_json(gate.LEDGER_PATH)
    ledger["requirements"][0] = {
        **ledger["requirements"][0],
        "status": "in_progress",
    }
    errors = gate.validate_ledger(ledger)
    assert "M0: verified milestone has non-verified requirements" in errors


def test_ledger_rejects_invalid_evidence_fields_paths_and_duplicates() -> None:
    gate = _load_gate()
    ledger = gate._load_json(gate.LEDGER_PATH)
    evidence = dict(ledger["requirements"][0]["evidence"][0])
    evidence["digest"] = "sha256:" + "A" * 64
    evidence["timestamp"] = "2026-02-30T00:00:00Z"
    evidence["result"] = "bad\nresult"
    ledger["requirements"][0] = {
        **ledger["requirements"][0],
        "evidence": [evidence],
    }
    ledger["milestones"][0] = {
        **ledger["milestones"][0],
        "evidence": ["../outside"],
    }
    ledger["authorized_paths"] = [
        *ledger["authorized_paths"],
        ledger["authorized_paths"][0],
    ]
    errors = gate.validate_ledger(ledger)
    assert "evidence ledger authorized_paths contains duplicates" in errors
    assert "M0: milestone evidence contains an unsafe or missing path" in errors
    assert "HARD-001: evidence[0].result must be bounded content-free text" in errors
    assert "HARD-001: evidence[0].digest must be empty or canonical SHA-256" in errors
    assert "HARD-001: evidence[0].timestamp must be a canonical UTC timestamp" in errors


def test_freeze_lift_and_ledger_policy_cannot_be_softened() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    ledger = gate._load_json(gate.LEDGER_PATH)
    freeze["lift"]["requires_signed_artifact_checks"] = False
    freeze["freeze"]["allowed_work"] = "all-work"
    ledger["base_commit"] = "deadbee"
    errors = gate.validate_freeze(freeze, ledger)
    assert "freeze.allowed_work must remain hardening-only" in errors
    assert "freeze and evidence ledger base commits do not match" in errors
    assert "lift.requires_signed_artifact_checks must remain true" in errors

    ledger = gate._load_json(gate.LEDGER_PATH)
    ledger["unexpected"] = True
    ledger["policy"]["uncertain_evidence"] = "pass"
    ledger["milestones"][0]["unexpected"] = True
    ledger["requirements"][0]["unexpected"] = True
    errors = gate.validate_ledger(ledger)
    assert "evidence ledger must use the exact top-level schema" in errors
    assert "evidence ledger uncertain evidence must remain not_verified" in errors
    assert "M0: milestone must use the exact schema" in errors
    assert "HARD-001: requirement must use the exact schema" in errors


def test_naming_allowlists_and_manifest_schemas_cannot_be_broadened() -> None:
    gate = _load_gate()
    freeze = gate._load_toml(gate.FREEZE_PATH)
    ledger = gate._load_json(gate.LEDGER_PATH)
    freeze["naming"]["process_names"].append("runtime")
    freeze["naming"]["classification"] = "caller-selected"
    freeze["lift"]["unexpected"] = True
    freeze["freeze"]["unexpected"] = True
    errors = gate.validate_freeze(freeze, ledger)
    assert "freeze manifest must use the exact [freeze] schema" in errors
    assert "freeze manifest must use the exact [lift] schema" in errors
    assert "naming classification must remain primary-responsibility" in errors
    assert "naming.process_names must match the audited allowlist" in errors


def test_ledger_text_rejects_unicode_direction_controls() -> None:
    gate = _load_gate()
    ledger = gate._load_json(gate.LEDGER_PATH)
    evidence = dict(ledger["requirements"][0]["evidence"][0])
    evidence["result"] = "pass\u202efail"
    ledger["requirements"][0] = {
        **ledger["requirements"][0],
        "evidence": [evidence],
    }
    errors = gate.validate_ledger(ledger)
    assert "HARD-001: evidence[0].result must be bounded content-free text" in errors


def test_release_event_is_blocked_while_freeze_is_active() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--release-event"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    assert "release event rejected" in completed.stderr


def test_github_release_environment_cannot_publish_during_freeze() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "release":
        return
    raise AssertionError("release event rejected: Algo CLI hardening freeze is active")


def test_publication_workflow_enforces_freeze_before_build_or_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "oliver-release.yml").read_text(
        encoding="utf-8"
    )
    gate = "python scripts/david_hardening_gate.py --release-event"
    assert gate in workflow
    assert workflow.index(gate) < workflow.index("python -m build")
    assert workflow.index(gate) < workflow.index("pypa/gh-action-pypi-publish@")


def test_external_actions_are_commit_pinned_and_release_attestations_are_present() -> None:
    workflows = [
        ROOT / ".github" / "workflows" / "henry-hardening-freeze.yml",
        ROOT / ".github" / "workflows" / "oliver-ci.yml",
        ROOT / ".github" / "workflows" / "oliver-release.yml",
    ]
    action = re.compile(r"^\s*-?\s*uses:\s*([^\s]+)@([^\s#]+)", re.MULTILINE)
    observed = []
    for path in workflows:
        observed.extend(action.findall(path.read_text(encoding="utf-8")))
    assert observed
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in observed)

    release = workflows[-1].read_text(encoding="utf-8")
    assert release.count("actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6") == 2
    assert "sbom-path:" in release
    assert "create-storage-record: false" in release


def test_ci_uses_frozen_lock_native_security_gates_and_separate_publish_artifacts() -> None:
    ci = (ROOT / ".github" / "workflows" / "oliver-ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "oliver-release.yml").read_text(
        encoding="utf-8"
    )
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    gateway_module = (ROOT / "harness-gateway" / "go.mod").read_text(
        encoding="utf-8"
    )
    assert "uv sync --frozen" in ci
    assert 'version: "0.11.26"' in ci
    assert "cargo clippy" in ci and "cargo audit" in ci
    assert "go test -race" in ci and "govulncheck ./..." in ci
    assert 'go-version: "1.26.5"' in ci
    assert "toolchain go1.26.5" in gateway_module
    assert "name: macOS native boundary" in ci
    assert "runs-on: macos-15" in ci
    assert "swift test --package-path native/austin" in ci
    assert "swift build --package-path native/austin --configuration release" in ci
    assert "./script/austin_build_and_run.sh neon-probe" in ci
    assert "./script/austin_build_and_run.sh migration-probe" in ci
    assert "algo_cli/ada_uninstall_recovery.py" in ci
    assert "pip-audit --local --strict" in ci
    assert "uv sync --frozen --no-editable" in ci
    assert "--cov=algo_cli" not in ci and "source_pkgs = [\"algo_cli\"]" in project
    assert "ada_supply_chain_manifest.py sbom" in ci
    assert "name: python-package-distributions" in release
    assert "name: release-supply-chain-evidence" in release
    publish = release.split("  publish:", 1)[1]
    assert "release-supply-chain-evidence" not in publish


def test_freeze_workflow_installs_and_uses_the_locked_environment() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "henry-hardening-freeze.yml"
    ).read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b" in workflow
    assert "uv sync --frozen --no-editable --extra dev" in workflow
    assert (
        "uv run --frozen --no-editable --extra dev python "
        "scripts/david_hardening_gate.py"
    ) in workflow
    assert "run: python scripts/david_hardening_gate.py --release-event" in workflow
    assert (
        "uv run --frozen --no-editable --extra dev pytest "
        "tests/test_david_hardening_gate.py -q"
    ) in workflow
    assert "run: python -m pytest" not in workflow


def test_ci_type_gate_includes_freeze_and_m8_evidence_scripts() -> None:
    ci = (ROOT / ".github" / "workflows" / "oliver-ci.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/david_hardening_gate.py" in ci
    assert "scripts/henry_m8_qualification.py" in ci
