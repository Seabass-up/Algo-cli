#!/usr/bin/env python3
"""Fail-closed Algo CLI hardening freeze and naming gate."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "hardening" / "henry-freeze.toml"
LEDGER_PATH = ROOT / "hardening" / "ada-evidence-ledger.json"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MILESTONE_ID_RE = re.compile(r"^M(?:0|[1-9][0-9]{0,2})$")
_REQUIREMENT_ID_RE = re.compile(r"^HARD-[0-9]{3}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_EVIDENCE_FIELDS = frozenset(
    {
        "kind",
        "path_or_command",
        "digest",
        "result",
        "timestamp",
        "scope",
        "limitations",
    }
)
_EVIDENCE_KINDS = frozenset(
    {
        "artifact",
        "audit",
        "benchmark",
        "command",
        "environment",
        "qualification",
        "runtime",
        "test",
        "workflow",
    }
)
_LEDGER_FIELDS = frozenset(
    {
        "schema_version",
        "freeze_id",
        "base_commit",
        "status",
        "policy",
        "authorized_paths",
        "milestones",
        "requirements",
    }
)
_POLICY_FIELDS = frozenset(
    {"allowed_change", "forbidden_change", "completion_rule", "uncertain_evidence"}
)
_MILESTONE_FIELDS = frozenset({"id", "name", "status", "evidence"})
_REQUIREMENT_FIELDS = frozenset({"id", "milestone", "summary", "status", "evidence"})
_FREEZE_FIELDS = frozenset(
    {
        "schema_version",
        "freeze_id",
        "status",
        "base_commit",
        "base_tag",
        "started_at",
        "reason",
        "allowed_work",
        "feature_development_blocked",
        "behavioral_refactors_blocked",
        "release_blocked",
        "tagging_blocked",
        "publishing_blocked",
    }
)
_LIFT_FIELDS = frozenset(
    {
        "requires_all_ledger_items_verified",
        "requires_full_test_matrix",
        "requires_security_fuzz_matrix",
        "requires_signed_artifact_checks",
        "requires_requirement_audit",
        "requires_explicit_lift_commit",
    }
)
_EXPECTED_NAMING: dict[str, tuple[str, ...]] = {
    "process_names": (
        "arthur",
        "david",
        "henry",
        "james",
        "marcus",
        "nathan",
        "oliver",
        "samuel",
        "theodore",
        "william",
    ),
    "memory_names": (
        "ada",
        "alice",
        "clara",
        "dorothy",
        "evelyn",
        "grace",
        "helen",
        "irene",
        "julia",
        "margaret",
    ),
    "browser_elements": (
        "argon",
        "boron",
        "carbon",
        "cobalt",
        "copper",
        "helium",
        "iron",
        "neon",
        "silicon",
        "xenon",
    ),
    "computer_cities": (
        "austin",
        "boston",
        "chicago",
        "denver",
        "london",
        "paris",
        "seattle",
        "sydney",
        "tokyo",
        "vienna",
    ),
}

CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "memory",
        (
            "memory",
            "ledger",
            "event_store",
            "artifact_store",
            "receipt_store",
            "telemetry",
            "supersession",
        ),
    ),
    (
        "browser",
        ("browser", "chrome", "chromium", "playwright", "webdriver", "extension"),
    ),
    (
        "computer",
        (
            "computer",
            "desktop",
            "macos",
            "accessibility",
            "screen_capture",
            "screencapture",
            "apple_event",
            "xpc",
            "tcc",
            "cg_event",
        ),
    ),
    (
        "process",
        (
            "process",
            "runtime",
            "dispatch",
            "effect",
            "approval",
            "capability",
            "control_kernel",
            "oneshot",
            "policy",
            "qos",
        ),
    ),
)

_RUNTIME_REGISTRY_PATHS = {
    "tools": ROOT / "algo_cli" / "tools.py",
    "actions": ROOT / "algo_cli" / "action_registry.py",
    "kernels": ROOT / "algo_cli" / "kernels" / "manifest.py",
    "slashes": ROOT / "algo_cli" / "oliver_slash_dispatch.py",
}
_RUNTIME_ENTRY_PATHS = (
    ROOT / "algo_cli" / "main.py",
    ROOT / "algo_cli" / "tools.py",
    ROOT / "algo_cli" / "nathan_runtime.py",
    ROOT / "algo_cli" / "nathan_program_runtime.py",
    ROOT / "algo_cli" / "oliver_slash_dispatch.py",
)
_DISABLED_RUNTIME_MODULE_MARKERS = (
    "austin_desktop",
    "austin_tcc",
    "austin_thomas",
    "boron_browser",
    "browser_use",
    "computer_use",
    "neon_browser",
    "xenon_browser",
)
_INTERACTIVE_NAMESPACES = frozenset(
    {
        "accessibility",
        "ax",
        "browser",
        "chrome",
        "chromium",
        "computer",
        "desktop",
        "dom",
        "keyboard",
        "mouse",
        "page",
        "screen",
        "ui",
    }
)
_INTERACTIVE_VERBS = frozenset(
    {
        "capture",
        "check",
        "click",
        "close",
        "control",
        "download",
        "drag",
        "fill",
        "focus",
        "goto",
        "keypress",
        "move",
        "navigate",
        "observe",
        "open",
        "press",
        "screenshot",
        "scroll",
        "select",
        "session",
        "snapshot",
        "submit",
        "type",
        "upload",
        "use",
    }
)
_FORBIDDEN_ACTIVATION_PATHS = (
    ROOT / "native" / "austin" / "Resources" / "AustinNativeControlActivation.json",
)


class GateError(RuntimeError):
    """A hardening invariant was violated."""


def _parse_python(path: Path) -> ast.Module:
    try:
        source = path.read_bytes().decode("utf-8-sig", errors="strict")
    except (OSError, UnicodeError) as error:
        raise GateError(f"cannot read runtime registry {path.relative_to(ROOT)}") from error
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as error:
        raise GateError(f"cannot parse runtime registry {path.relative_to(ROOT)}") from error


def _single_assignment(tree: ast.Module, name: str, path: Path) -> ast.AST:
    matches: list[ast.AST] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is not None:
                matches.append(node.value)
    if len(matches) != 1:
        relative = path.relative_to(ROOT)
        raise GateError(f"{relative}: expected one static {name} assignment")
    return matches[0]


def _sequence(value: ast.AST, *, label: str) -> list[ast.AST]:
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise GateError(f"{label} must remain a static list or tuple")
    return list(value.elts)


def _tool_names(path: Path) -> set[str]:
    tree = _parse_python(path)
    entries = _sequence(_single_assignment(tree, "ALL_TOOLS", path), label="ALL_TOOLS")
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, ast.Name):
            names.add(entry.id)
            continue
        if (
            isinstance(entry, ast.Call)
            and isinstance(entry.func, ast.Name)
            and entry.func.id == "_hide_cfg_param"
            and len(entry.args) == 1
            and not entry.keywords
            and isinstance(entry.args[0], ast.Name)
        ):
            names.add(entry.args[0].id)
            continue
        raise GateError("ALL_TOOLS contains a non-static or unrecognized entry")
    if not names:
        raise GateError("ALL_TOOLS must not be empty")
    return names


def _slash_names(path: Path) -> set[str]:
    tree = _parse_python(path)
    entries = _sequence(
        _single_assignment(tree, "SLASH_COMMANDS", path), label="SLASH_COMMANDS"
    )
    names: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, ast.Tuple)
            or len(entry.elts) != 2
            or not isinstance(entry.elts[0], ast.Constant)
            or type(entry.elts[0].value) is not str
        ):
            raise GateError("SLASH_COMMANDS contains a non-static or malformed entry")
        names.add(entry.elts[0].value)
    if not names:
        raise GateError("SLASH_COMMANDS must not be empty")
    return names


def _action_spec_names(path: Path) -> set[str]:
    tree = _parse_python(path)
    entries = _sequence(
        _single_assignment(tree, "ACTION_SPECS", path), label="ACTION_SPECS"
    )
    names: set[str] = set()
    for node in entries:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_spec"
        ):
            raise GateError("ACTION_SPECS contains a non-static or unrecognized entry")
        if (
            not node.args
            or not isinstance(node.args[0], ast.Constant)
            or type(node.args[0].value) is not str
        ):
            raise GateError("ACTION_SPECS contains a non-static action name")
        names.add(node.args[0].value)
    if not names:
        raise GateError("ACTION_SPECS must contain static action names")
    return names


def _kernel_action_names(path: Path) -> set[str]:
    tree = _parse_python(path)
    entries = _sequence(_single_assignment(tree, "_KERNELS", path), label="_KERNELS")
    names: set[str] = set()
    for node in entries:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "KernelSpec"
        ):
            raise GateError("_KERNELS contains a non-static or unrecognized entry")
        actions = [keyword.value for keyword in node.keywords if keyword.arg == "actions"]
        if len(actions) != 1:
            raise GateError("KernelSpec must contain exactly one static actions field")
        for entry in _sequence(actions[0], label="KernelSpec.actions"):
            if not isinstance(entry, ast.Constant) or type(entry.value) is not str:
                raise GateError("KernelSpec.actions contains a non-static action name")
            names.add(entry.value)
    if not names:
        raise GateError("kernel manifest must contain static action names")
    return names


def _runtime_imports(path: Path) -> set[str]:
    tree = _parse_python(path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            imports.add(prefix)
            imports.update(f"{prefix}.{alias.name}".strip(".") for alias in node.names)
    return imports


def _interactive_capability_name(name: str) -> bool:
    normalized = name.casefold().strip()
    root = normalized.split(maxsplit=1)[0]
    if root.startswith("/") and root[1:] in _INTERACTIVE_NAMESPACES:
        return True
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", normalized)
        if token
    }
    return bool(tokens & _INTERACTIVE_NAMESPACES and tokens & _INTERACTIVE_VERBS)


def validate_unqualified_capability_exposure(
    *,
    registries: dict[str, set[str]] | None = None,
    runtime_imports: dict[str, set[str]] | None = None,
    activation_paths: tuple[Path, ...] | None = None,
) -> list[str]:
    """Reject Browser/Computer Use reachability while the freeze is active."""

    try:
        observed = (
            {
                "tool": _tool_names(_RUNTIME_REGISTRY_PATHS["tools"]),
                "action": _action_spec_names(_RUNTIME_REGISTRY_PATHS["actions"]),
                "kernel": _kernel_action_names(_RUNTIME_REGISTRY_PATHS["kernels"]),
                "slash": _slash_names(_RUNTIME_REGISTRY_PATHS["slashes"]),
            }
            if registries is None
            else registries
        )
        imported = (
            {
                str(path.relative_to(ROOT)): _runtime_imports(path)
                for path in _RUNTIME_ENTRY_PATHS
            }
            if runtime_imports is None
            else runtime_imports
        )
    except GateError as error:
        return [str(error)]

    errors = [
        f"{surface} exposes unqualified interactive capability {name!r} during freeze"
        for surface, names in sorted(observed.items())
        for name in sorted(names)
        if _interactive_capability_name(name)
    ]
    for source, modules in sorted(imported.items()):
        for module in sorted(modules):
            normalized = module.casefold().replace("-", "_")
            if any(marker in normalized for marker in _DISABLED_RUNTIME_MODULE_MARKERS):
                errors.append(
                    f"{source} imports disabled capability module {module!r} during freeze"
                )
    paths = _FORBIDDEN_ACTIVATION_PATHS if activation_paths is None else activation_paths
    for path in paths:
        if path.exists():
            try:
                relative = path.relative_to(ROOT)
            except ValueError:
                relative = path
            errors.append(
                f"{relative}: production computer-control activation is forbidden during freeze"
            )
    return errors


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise GateError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def _run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _tokenize_basename(path: str) -> set[str]:
    stem = Path(path).stem
    stem = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stem)
    stem = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", stem).casefold()
    return {token for token in re.split(r"[^a-z0-9]+", stem) if token}


def classify_path(path: str) -> str | None:
    normalized = path.casefold().replace("-", "_")
    for category, patterns in CATEGORY_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return category
    return None


def _allowed_tokens(freeze: dict[str, Any], category: str) -> set[str]:
    naming = freeze.get("naming")
    if not isinstance(naming, dict):
        raise GateError("freeze manifest is missing [naming]")
    key = {
        "process": "process_names",
        "memory": "memory_names",
        "browser": "browser_elements",
        "computer": "computer_cities",
    }[category]
    values = naming.get(key)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise GateError(f"freeze manifest has invalid naming.{key}")
    return {value.casefold() for value in values}


def validate_filename(path: str, freeze: dict[str, Any]) -> str | None:
    category = classify_path(path)
    if category is None:
        return None
    if _tokenize_basename(path) & _allowed_tokens(freeze, category):
        return None
    return f"{path}: {category} file lacks an approved {_naming_label(category)} token"


def _naming_label(category: str) -> str:
    return {
        "process": "male-name",
        "memory": "female-name",
        "browser": "element-name",
        "computer": "city-name",
    }[category]


def _bounded_plain_text(value: Any, *, maximum: int) -> bool:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or _CONTROL_RE.search(value) is not None
    ):
        return False
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return 1 <= size <= maximum


def _safe_repository_path(value: Any, *, require_exists: bool) -> bool:
    if not _bounded_plain_text(value, maximum=512) or "\\" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        return False
    if not require_exists:
        return True
    candidate = ROOT
    try:
        for part in path.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return False
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(ROOT.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _valid_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _validate_evidence(requirement_id: str, evidence: Any, index: int) -> list[str]:
    prefix = f"{requirement_id}: evidence[{index}]"
    if not isinstance(evidence, dict):
        return [f"{prefix} must be an object"]
    if set(evidence) != _EVIDENCE_FIELDS:
        return [f"{prefix} must use the exact evidence schema"]
    errors: list[str] = []
    if evidence.get("kind") not in _EVIDENCE_KINDS:
        errors.append(f"{prefix} has an invalid kind")
    for field in ("path_or_command", "result", "scope", "limitations"):
        if not _bounded_plain_text(evidence.get(field), maximum=4096):
            errors.append(f"{prefix}.{field} must be bounded content-free text")
    digest = evidence.get("digest")
    if not isinstance(digest, str) or (digest and _DIGEST_RE.fullmatch(digest) is None):
        errors.append(f"{prefix}.digest must be empty or canonical SHA-256")
    if not _valid_utc_timestamp(evidence.get("timestamp")):
        errors.append(f"{prefix}.timestamp must be a canonical UTC timestamp")
    return errors


def _changed_paths(base_commit: str) -> set[str]:
    committed = {
        line.strip()
        for line in _run_git("diff", "--name-only", "--diff-filter=ACMR", f"{base_commit}...HEAD").splitlines()
        if line.strip()
    }
    porcelain = _run_git("status", "--porcelain=v1", "--untracked-files=all")
    working: set[str] = set()
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        if "D" in status:
            # A deletion creates no newly named artifact. The replacement path,
            # when any, is independently observed as renamed or untracked.
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        working.add(path.strip().strip('"'))
    return committed | working


def validate_freeze(freeze: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    freeze_section = freeze.get("freeze")
    if not isinstance(freeze_section, dict):
        return ["freeze manifest is missing [freeze]"]
    if set(freeze_section) != _FREEZE_FIELDS:
        errors.append("freeze manifest must use the exact [freeze] schema")
    if freeze_section.get("schema_version") != 1 or type(
        freeze_section.get("schema_version")
    ) is not int:
        errors.append("freeze.schema_version must be exactly 1")
    if freeze_section.get("status") != "active":
        errors.append("hardening freeze must remain active until the audited lift milestone")
    if freeze_section.get("allowed_work") != "hardening-only":
        errors.append("freeze.allowed_work must remain hardening-only")
    for key in (
        "feature_development_blocked",
        "behavioral_refactors_blocked",
        "release_blocked",
        "tagging_blocked",
        "publishing_blocked",
    ):
        if freeze_section.get(key) is not True:
            errors.append(f"freeze.{key} must be true")
    if ledger.get("freeze_id") != freeze_section.get("freeze_id"):
        errors.append("freeze and evidence ledger IDs do not match")
    if ledger.get("base_commit") != freeze_section.get("base_commit"):
        errors.append("freeze and evidence ledger base commits do not match")
    if ledger.get("status") != "active":
        errors.append("evidence ledger must remain active until the audited lift")
    lift = freeze.get("lift")
    if not isinstance(lift, dict):
        errors.append("freeze manifest is missing [lift]")
    else:
        if set(lift) != _LIFT_FIELDS:
            errors.append("freeze manifest must use the exact [lift] schema")
        for key in _LIFT_FIELDS:
            if lift.get(key) is not True:
                errors.append(f"lift.{key} must remain true")
    naming = freeze.get("naming")
    if not isinstance(naming, dict) or set(naming) != set(_EXPECTED_NAMING) | {
        "enforce_on_created_and_modified_files",
        "classification",
    }:
        errors.append("freeze manifest must use the exact [naming] schema")
    else:
        if naming.get("enforce_on_created_and_modified_files") is not True:
            errors.append("naming enforcement must remain enabled")
        if naming.get("classification") != "primary-responsibility":
            errors.append("naming classification must remain primary-responsibility")
        for key, expected in _EXPECTED_NAMING.items():
            value = naming.get(key)
            if not isinstance(value, list) or tuple(value) != expected:
                errors.append(f"naming.{key} must match the audited allowlist")
    return errors


def validate_ledger(ledger: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(ledger) != _LEDGER_FIELDS:
        errors.append("evidence ledger must use the exact top-level schema")
    if ledger.get("schema_version") != 1 or type(ledger.get("schema_version")) is not int:
        errors.append("evidence ledger schema_version must be exactly 1")
    if not _bounded_plain_text(ledger.get("freeze_id"), maximum=128):
        errors.append("evidence ledger freeze_id must be bounded ASCII text")
    base_commit = ledger.get("base_commit")
    if not isinstance(base_commit, str) or re.fullmatch(r"[0-9a-f]{7,40}", base_commit) is None:
        errors.append("evidence ledger base_commit must be canonical hexadecimal")
    policy = ledger.get("policy")
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        errors.append("evidence ledger policy must use the exact policy schema")
    else:
        for key in ("allowed_change", "forbidden_change", "completion_rule"):
            if not _bounded_plain_text(policy.get(key), maximum=1024):
                errors.append(f"evidence ledger policy.{key} must be bounded ASCII text")
        if policy.get("uncertain_evidence") != "not_verified":
            errors.append("evidence ledger uncertain evidence must remain not_verified")
    requirements = ledger.get("requirements")
    milestones = ledger.get("milestones")
    if not isinstance(requirements, list) or not requirements:
        errors.append("evidence ledger must contain requirements")
        return errors
    if not isinstance(milestones, list) or not milestones:
        errors.append("evidence ledger must contain milestones")
        return errors
    authorized_paths = ledger.get("authorized_paths")
    if not isinstance(authorized_paths, list) or not authorized_paths:
        errors.append("evidence ledger authorized_paths must be a non-empty list")
    elif any(not _safe_repository_path(path, require_exists=False) for path in authorized_paths):
        errors.append("evidence ledger authorized_paths contains an unsafe path")
    elif len(set(authorized_paths)) != len(authorized_paths):
        errors.append("evidence ledger authorized_paths contains duplicates")

    milestone_ids: set[str] = set()
    milestone_statuses: dict[str, str] = {}
    allowed_status = {"pending", "in_progress", "verified", "not_verified"}
    for row in milestones:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append("every milestone must be an object with an ID")
            continue
        milestone_id = row["id"]
        if set(row) != _MILESTONE_FIELDS:
            errors.append(f"{milestone_id}: milestone must use the exact schema")
        if _MILESTONE_ID_RE.fullmatch(milestone_id) is None:
            errors.append(f"invalid milestone ID: {milestone_id}")
        if milestone_id in milestone_ids:
            errors.append(f"duplicate milestone ID: {milestone_id}")
        milestone_ids.add(milestone_id)
        if not _bounded_plain_text(row.get("name"), maximum=256):
            errors.append(f"{milestone_id}: milestone name must be bounded text")
        status = row.get("status")
        if status not in allowed_status:
            errors.append(f"{milestone_id}: invalid milestone status {status!r}")
        else:
            milestone_statuses[milestone_id] = status
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{milestone_id}: milestone evidence must be a list")
        elif status == "verified" and not evidence:
            errors.append(f"{milestone_id}: verified milestone has no evidence")
        elif any(not _safe_repository_path(path, require_exists=True) for path in evidence):
            errors.append(f"{milestone_id}: milestone evidence contains an unsafe or missing path")
        elif len(set(evidence)) != len(evidence):
            errors.append(f"{milestone_id}: milestone evidence contains duplicates")
    seen: set[str] = set()
    requirements_by_milestone: dict[str, list[str]] = {
        milestone_id: [] for milestone_id in milestone_ids
    }
    for row in requirements:
        if not isinstance(row, dict):
            errors.append("every requirement must be an object")
            continue
        requirement_id = row.get("id")
        if not isinstance(requirement_id, str) or not requirement_id:
            errors.append("every requirement must have an ID")
            continue
        if _REQUIREMENT_ID_RE.fullmatch(requirement_id) is None:
            errors.append(f"invalid requirement ID: {requirement_id}")
        if set(row) != _REQUIREMENT_FIELDS:
            errors.append(f"{requirement_id}: requirement must use the exact schema")
        if requirement_id in seen:
            errors.append(f"duplicate requirement ID: {requirement_id}")
        seen.add(requirement_id)
        milestone_id = row.get("milestone")
        if milestone_id not in milestone_ids:
            errors.append(f"{requirement_id}: unknown milestone {milestone_id!r}")
        else:
            requirements_by_milestone[milestone_id].append(requirement_id)
        if not _bounded_plain_text(row.get("summary"), maximum=512):
            errors.append(f"{requirement_id}: summary must be bounded text")
        status = row.get("status")
        if status not in allowed_status:
            errors.append(f"{requirement_id}: invalid status {status!r}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{requirement_id}: evidence must be a list")
        elif status == "verified" and not evidence:
            errors.append(f"{requirement_id}: verified without evidence")
        elif isinstance(evidence, list):
            for index, item in enumerate(evidence):
                errors.extend(_validate_evidence(requirement_id, item, index))

    requirement_status = {
        row.get("id"): row.get("status")
        for row in requirements
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for milestone_id, requirement_ids in requirements_by_milestone.items():
        if not requirement_ids:
            errors.append(f"{milestone_id}: milestone has no requirements")
            continue
        milestone_status = milestone_statuses.get(milestone_id)
        statuses = {requirement_status.get(requirement_id) for requirement_id in requirement_ids}
        if milestone_status == "verified" and statuses != {"verified"}:
            errors.append(f"{milestone_id}: verified milestone has non-verified requirements")
        if milestone_status == "pending" and statuses != {"pending"}:
            errors.append(f"{milestone_id}: pending milestone has started requirements")
    return errors


def validate_changed_paths(
    freeze: dict[str, Any], ledger: dict[str, Any], *, changed_paths: set[str] | None = None
) -> list[str]:
    freeze_section = freeze.get("freeze")
    if not isinstance(freeze_section, dict):
        return ["freeze manifest is missing [freeze]"]
    base_commit = freeze_section.get("base_commit")
    if not isinstance(base_commit, str) or not base_commit:
        return ["freeze.base_commit must be a non-empty string"]
    paths = _changed_paths(base_commit) if changed_paths is None else changed_paths
    authorized_raw = ledger.get("authorized_paths")
    if not isinstance(authorized_raw, list) or not all(isinstance(path, str) for path in authorized_raw):
        return ["evidence ledger authorized_paths must be a list of strings"]
    authorized = set(authorized_raw)
    errors: list[str] = []
    for path in sorted(paths):
        if path not in authorized:
            errors.append(f"{path}: changed during freeze without ledger authorization")
        naming_error = validate_filename(path, freeze)
        if naming_error:
            errors.append(naming_error)
    return errors


def run_gate(*, changed_paths: set[str] | None = None) -> list[str]:
    freeze = _load_toml(FREEZE_PATH)
    ledger = _load_json(LEDGER_PATH)
    return [
        *validate_freeze(freeze, ledger),
        *validate_ledger(ledger),
        *validate_changed_paths(freeze, ledger, changed_paths=changed_paths),
        *validate_unqualified_capability_exposure(),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-event",
        action="store_true",
        help="also assert that a release is prohibited while the freeze is active",
    )
    args = parser.parse_args(argv)

    errors = run_gate()
    freeze = _load_toml(FREEZE_PATH)
    freeze_section = freeze.get("freeze", {})
    release_event = args.release_event or os.environ.get("GITHUB_EVENT_NAME") == "release"
    if release_event and isinstance(freeze_section, dict) and freeze_section.get("release_blocked") is True:
        errors.append("release event rejected: Algo CLI hardening freeze is active")

    if errors:
        print("Algo CLI hardening gate: BLOCKED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Algo CLI hardening gate: PASS (freeze active, hardening-only changes authorized)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
