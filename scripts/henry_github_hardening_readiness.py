#!/usr/bin/env python3
"""Assess protected GitHub signing readiness without mutating remote state."""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any, Mapping, NoReturn
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REPOSITORY_ID = 1_297_752_684
ENVIRONMENT_NAME = "native-hardening"
WORKFLOW_PATH = ".github/workflows/henry-austin-signing-qualification.yml"
EPHEMERAL_RUNNER_LABEL = "algo-cli-signing-ephemeral"
LEGACY_RUNNER_LABEL = "algo-cli-signing"
RUNNER_GROUP_NAME = "algo-cli-signing"
REQUIRED_RUNNER_LABELS = frozenset({"self-hosted", "macOS", "ARM64", EPHEMERAL_RUNNER_LABEL})
REQUIRED_SECRETS = frozenset(
    {
        "AUSTIN_APPLICATION_IDENTITY",
        "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL",
        "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_SHA256",
        "AUSTIN_EXTENSION_ORIGIN",
        "AUSTIN_INSTALLER_IDENTITY",
        "AUSTIN_NOTARY_PROFILE",
        "AUSTIN_RUNNER_ATTESTATION_SHA256",
        "AUSTIN_TEAM_ID",
    }
)
MAX_API_BYTES = 2 * 1024 * 1024
MAX_WORKFLOW_BYTES = 256 * 1024
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9._-])?$")
_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\.ya?ml$")
_REGISTERED_WORKFLOW_PATH_RE = re.compile(r"^[.A-Za-z0-9][A-Za-z0-9_./-]{0,254}$")
_GITHUB_HOSTED_RUNNER_RE = re.compile(r"^(?:ubuntu|windows|macos)-(?:latest|[0-9][A-Za-z0-9.-]{0,31})$")
_RUNS_ON_RE = re.compile(r"(?m)^\s*runs-on:\s*([^#\r\n]+?)\s*$")
_EMPTY_RUNS_ON_RE = re.compile(r"(?m)^\s*runs-on:\s*(?:#.*)?$")
_MATRIX_OS_ITEM_RE = re.compile(r"(?m)^\s*-\s+os:\s*([^#\s\r\n]+)")
_MATRIX_OS_LIST_RE = re.compile(r"(?m)^\s*os:\s*\[([^\]\r\n]+)\]\s*$")
_DYNAMIC_ENVIRONMENT_RE = re.compile(r"(?m)^\s*environment:\s*\$\{\{")
_MATRIX_RUNNER = "${{ matrix.os }}"
_PLATFORM_WORKFLOW_PATHS = frozenset(
    {
        "dynamic/dependabot/dependabot-updates",
        "dynamic/dependabot/update-graph",
    }
)


class GitHubHardeningReadinessRejected(RuntimeError):
    """Remote readiness evidence was malformed or could not be collected safely."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "github_readiness_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise GitHubHardeningReadinessRejected(reason_code)


def _mapping(value: object, reason_code: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        _reject(reason_code)
    return value


def _array(value: object, reason_code: str) -> list[Any]:
    if type(value) is not list:
        _reject(reason_code)
    return value


def _safe_ref(value: object) -> str:
    if (
        type(value) is not str
        or _REF_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith(".lock")
    ):
        _reject("github_readiness_ref")
    return value


def _safe_file(path: Path, *, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _reject("github_readiness_workflow")
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum
    ):
        _reject("github_readiness_workflow")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size)
        ):
            _reject("github_readiness_workflow")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                _reject("github_readiness_workflow")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            _reject("github_readiness_workflow")
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _reject("github_readiness_workflow")
        return bytes(payload)
    except OSError:
        _reject("github_readiness_workflow")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _local_workflow_inventory(directory: Path) -> dict[str, bytes]:
    try:
        entries = sorted(directory.iterdir(), key=lambda value: value.name)
    except OSError:
        _reject("github_readiness_workflow")
    if not 1 <= len(entries) <= 100:
        _reject("github_readiness_workflow")
    result: dict[str, bytes] = {}
    for path in entries:
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            _reject("github_readiness_workflow")
        if _WORKFLOW_PATH_RE.fullmatch(relative) is None or relative in result:
            _reject("github_readiness_workflow")
        result[relative] = _safe_file(path, maximum=MAX_WORKFLOW_BYTES)
    if WORKFLOW_PATH not in result:
        _reject("github_readiness_workflow")
    return result


def _workflow_text(payload: bytes) -> str:
    try:
        value = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("github_readiness_workflow")
    if not value or "\x00" in value or "\r" in value:
        _reject("github_readiness_workflow")
    return value


def _matrix_os_values(workflow: str) -> set[str]:
    values = {match.group(1).strip("'\"") for match in _MATRIX_OS_ITEM_RE.finditer(workflow)}
    for match in _MATRIX_OS_LIST_RE.finditer(workflow):
        for value in match.group(1).split(","):
            selected = value.strip().strip("'\"")
            if selected:
                values.add(selected)
    return values


def _has_ambiguous_environment(workflow: str) -> bool:
    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)environment:\s*(.*?)\s*$", line)
        if match is None:
            continue
        value = match.group(2)
        if value:
            if "${{" in value:
                return True
            continue
        parent_indent = len(match.group(1))
        fixed_name = False
        for child in lines[index + 1 :]:
            stripped = child.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            child_indent = len(child) - len(stripped)
            if child_indent <= parent_indent:
                break
            name_match = re.match(r"^name:\s*(.*?)\s*$", stripped)
            if name_match is None:
                continue
            name = name_match.group(1).strip("'\"")
            if not name or "${{" in name:
                return True
            fixed_name = True
        if not fixed_name:
            return True
    return False


def _exclusive_signing_authority(workflows: Mapping[str, bytes]) -> bool:
    if set(workflows).difference({WORKFLOW_PATH}) == set():
        return True
    privileged_markers = (
        "self-hosted",
        ENVIRONMENT_NAME,
        "algo-cli-signing",
        *sorted(REQUIRED_SECRETS),
    )
    for path, payload in workflows.items():
        if path == WORKFLOW_PATH:
            continue
        text = _workflow_text(payload)
        if any(marker in text for marker in privileged_markers):
            return False
        if (
            _DYNAMIC_ENVIRONMENT_RE.search(text) is not None
            or _has_ambiguous_environment(text)
            or _EMPTY_RUNS_ON_RE.search(text) is not None
        ):
            return False
        for match in _RUNS_ON_RE.finditer(text):
            runner = match.group(1).strip().strip("'\"")
            if runner == _MATRIX_RUNNER:
                matrix_values = _matrix_os_values(text)
                if not matrix_values or any(
                    _GITHUB_HOSTED_RUNNER_RE.fullmatch(value) is None for value in matrix_values
                ):
                    return False
            elif _GITHUB_HOSTED_RUNNER_RE.fullmatch(runner) is None:
                return False
    return True


def _signing_workflow_contract(payload: bytes) -> bool:
    text = _workflow_text(payload)
    trigger = text.split("permissions:\n", 1)[0]
    required_fragments = {
        "workflow_dispatch:",
        "environment: native-hardening",
        "group: algo-cli-signing",
        "labels: [self-hosted, macOS, ARM64, algo-cli-signing-ephemeral]",
        "github.repository_id != '1297752684'",
        "github.ref != format('refs/heads/{0}', github.event.repository.default_branch)",
        "AUSTIN_BUILD_NUMBER: ${{ github.run_number }}",
        '--build-number "${AUSTIN_BUILD_NUMBER}"',
        "/usr/bin/python3 scripts/henry_austin_signing_runner.py",
    }
    required_fragments.update(f"secrets.{name}" for name in REQUIRED_SECRETS)
    try:
        protected_guard = text.index("if: ${{ github.repository_id != '1297752684' ||")
        checkout = text.index("actions/checkout@")
        runner_preflight = text.index("/usr/bin/python3 scripts/henry_austin_signing_runner.py")
        setup_python = text.index("actions/setup-python@")
        setup_uv = text.index("astral-sh/setup-uv@")
        dependency_install = text.index("uv sync --frozen")
    except ValueError:
        return False
    return (
        all(fragment in text for fragment in required_fragments)
        and protected_guard < checkout < runner_preflight < setup_python < setup_uv < dependency_install
        and "inputs:" not in trigger
        and "inputs." not in text
        and "github.event.inputs" not in text
        and "--version" not in text
    )


def _run_gh(gh_binary: str, endpoint: str) -> Any:
    environment = dict(os.environ)
    environment.update(
        {
            "GH_FORCE_TTY": "0",
            "GH_PROMPT_DISABLED": "1",
            "PAGER": "cat",
        }
    )
    try:
        completed = subprocess.run(
            [gh_binary, "api", endpoint],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        _reject("github_readiness_api")
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > MAX_API_BYTES
        or len(completed.stderr) > 64 * 1024
    ):
        _reject("github_readiness_api")
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("github_readiness_api")


def _bounded_collection(payload: Mapping[str, Any], member: str) -> list[Any]:
    values = _array(payload.get(member), "github_readiness_api_schema")
    total = payload.get("total_count")
    if type(total) is not int or total < 0 or total != len(values) or total > 100:
        _reject("github_readiness_api_pagination")
    return values


def collect_snapshot(*, repository: str, gh_binary: str) -> dict[str, Any]:
    if _REPOSITORY_RE.fullmatch(repository) is None:
        _reject("github_readiness_repository")
    repo = _mapping(
        _run_gh(gh_binary, f"repos/{repository}"),
        "github_readiness_api_schema",
    )
    owner = _mapping(repo.get("owner"), "github_readiness_api_schema")
    expected_owner = repository.split("/", 1)[0]
    owner_login = owner.get("login")
    owner_type = owner.get("type")
    if owner_login != expected_owner or owner_type not in {"Organization", "User"}:
        _reject("github_readiness_repository")
    default_branch = _safe_ref(repo.get("default_branch"))
    encoded_ref = quote(default_branch, safe="")
    branch = _mapping(
        _run_gh(gh_binary, f"repos/{repository}/branches/{encoded_ref}"),
        "github_readiness_api_schema",
    )
    environments = _mapping(
        _run_gh(gh_binary, f"repos/{repository}/environments?per_page=100"),
        "github_readiness_api_schema",
    )
    environment: Mapping[str, Any] | None = None
    for candidate_value in _bounded_collection(environments, "environments"):
        candidate = _mapping(candidate_value, "github_readiness_api_schema")
        if candidate.get("name") == ENVIRONMENT_NAME:
            if environment is not None:
                _reject("github_readiness_environment")
            environment = candidate
    if environment is None:
        environment_secrets: Mapping[str, Any] = {"secrets": [], "total_count": 0}
    else:
        environment = _mapping(
            _run_gh(
                gh_binary,
                f"repos/{repository}/environments/{ENVIRONMENT_NAME}",
            ),
            "github_readiness_api_schema",
        )
        environment_secrets = _mapping(
            _run_gh(
                gh_binary,
                f"repos/{repository}/environments/{ENVIRONMENT_NAME}/secrets?per_page=100",
            ),
            "github_readiness_api_schema",
        )
    runner_group: Mapping[str, Any] | None = None
    runner_group_repositories: Mapping[str, Any] = {
        "repositories": [],
        "total_count": 0,
    }
    runners: Mapping[str, Any] = {"runners": [], "total_count": 0}
    if owner_type == "Organization":
        groups = _mapping(
            _run_gh(
                gh_binary,
                f"orgs/{expected_owner}/actions/runner-groups?per_page=100",
            ),
            "github_readiness_api_schema",
        )
        for candidate_value in _bounded_collection(groups, "runner_groups"):
            candidate = _mapping(candidate_value, "github_readiness_api_schema")
            if candidate.get("name") == RUNNER_GROUP_NAME:
                if runner_group is not None:
                    _reject("github_readiness_runner_group")
                runner_group = candidate
        if runner_group is not None:
            group_id = runner_group.get("id")
            if type(group_id) is not int or group_id <= 0:
                _reject("github_readiness_runner_group")
            if runner_group.get("visibility") == "selected":
                runner_group_repositories = _mapping(
                    _run_gh(
                        gh_binary,
                        f"orgs/{expected_owner}/actions/runner-groups/{group_id}/repositories?per_page=100",
                    ),
                    "github_readiness_api_schema",
                )
            runners = _mapping(
                _run_gh(
                    gh_binary,
                    f"orgs/{expected_owner}/actions/runner-groups/{group_id}/runners?per_page=100",
                ),
                "github_readiness_api_schema",
            )
    workflows = _mapping(
        _run_gh(gh_binary, f"repos/{repository}/actions/workflows?per_page=100"),
        "github_readiness_api_schema",
    )
    directory_value = _run_gh(
        gh_binary,
        f"repos/{repository}/contents/.github/workflows?ref={encoded_ref}",
    )
    directory = _array(directory_value, "github_readiness_api_schema")
    if not 1 <= len(directory) <= 100:
        _reject("github_readiness_workflow")
    remote_workflow_files: dict[str, Mapping[str, Any]] = {}
    for entry_value in directory:
        entry = _mapping(entry_value, "github_readiness_api_schema")
        path = entry.get("path")
        if (
            type(path) is not str
            or _WORKFLOW_PATH_RE.fullmatch(path) is None
            or entry.get("type") != "file"
            or path in remote_workflow_files
        ):
            _reject("github_readiness_workflow")
        remote_workflow_files[path] = _mapping(
            _run_gh(
                gh_binary,
                f"repos/{repository}/contents/{path}?ref={encoded_ref}",
            ),
            "github_readiness_api_schema",
        )
    return {
        "branch": branch,
        "environment": environment,
        "environment_secrets": environment_secrets,
        "remote_workflow_files": remote_workflow_files,
        "repository": repo,
        "runner_group": runner_group,
        "runner_group_repositories": runner_group_repositories,
        "runners": runners,
        "workflows": workflows,
    }


def _remote_workflow_payload(value: object, *, expected_path: str) -> bytes:
    if _WORKFLOW_PATH_RE.fullmatch(expected_path) is None:
        _reject("github_readiness_workflow")
    remote = _mapping(value, "github_readiness_workflow")
    if remote.get("encoding") != "base64" or remote.get("path") != expected_path:
        _reject("github_readiness_workflow")
    encoded = remote.get("content")
    if type(encoded) is not str:
        _reject("github_readiness_workflow")
    compact = "".join(encoded.splitlines())
    if not compact or len(compact) > (MAX_WORKFLOW_BYTES * 2):
        _reject("github_readiness_workflow")
    try:
        payload = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        _reject("github_readiness_workflow")
    if not 1 <= len(payload) <= MAX_WORKFLOW_BYTES or base64.b64encode(payload).decode("ascii") != compact:
        _reject("github_readiness_workflow")
    _workflow_text(payload)
    return payload


def assess_snapshot(
    snapshot: Mapping[str, Any],
    *,
    repository: str,
    expected_workflow: Path,
) -> dict[str, Any]:
    if type(snapshot) is not dict or _REPOSITORY_RE.fullmatch(repository) is None:
        _reject("github_readiness_snapshot")
    local_workflows = _local_workflow_inventory(expected_workflow.parent)
    local_workflow = local_workflows[WORKFLOW_PATH]
    local_digest = "sha256:" + hashlib.sha256(local_workflow).hexdigest()
    repo = _mapping(snapshot.get("repository"), "github_readiness_api_schema")
    if repo.get("full_name") != repository:
        _reject("github_readiness_repository")
    default_branch = _safe_ref(repo.get("default_branch"))
    visibility = repo.get("visibility")
    if visibility not in {"public", "private", "internal"}:
        _reject("github_readiness_repository")
    owner = _mapping(repo.get("owner"), "github_readiness_repository")
    expected_owner = repository.split("/", 1)[0]
    owner_is_organization = owner.get("login") == expected_owner and owner.get("type") == "Organization"
    repository_identity_matches = repo.get("id") == EXPECTED_REPOSITORY_ID

    checks: list[dict[str, Any]] = []

    def record(check_id: str, passed: bool, observed: Mapping[str, Any]) -> None:
        checks.append(
            {
                "id": check_id,
                "observed": dict(observed),
                "status": "passed" if passed else "blocked",
            }
        )

    branch = _mapping(snapshot.get("branch"), "github_readiness_api_schema")
    record(
        "default_branch_protected",
        branch.get("name") == default_branch and branch.get("protected") is True,
        {"protected": branch.get("protected") is True},
    )
    record(
        "pinned_repository_identity",
        repository_identity_matches,
        {"matches_pinned_id": repository_identity_matches},
    )
    record(
        "organization_owned_repository",
        owner_is_organization,
        {"organization_owned": owner_is_organization},
    )

    environment_value = snapshot.get("environment")
    environment = None if environment_value is None else _mapping(environment_value, "github_readiness_environment")
    environment_exists = environment is not None and environment.get("name") == ENVIRONMENT_NAME
    record("native_hardening_environment", environment_exists, {"exists": environment_exists})

    if environment is None:
        rules: list[Any] = []
        policy: Mapping[str, Any] = {}
        can_admins_bypass: object = None
    else:
        rules = _array(environment.get("protection_rules"), "github_readiness_environment")
        policy_value = environment.get("deployment_branch_policy")
        policy = {} if policy_value is None else _mapping(policy_value, "github_readiness_environment")
        can_admins_bypass = environment.get("can_admins_bypass")
    record(
        "environment_non_bypassable",
        can_admins_bypass is False,
        {"admins_can_bypass": (can_admins_bypass if type(can_admins_bypass) is bool else None)},
    )
    branch_policy_ok = policy.get("protected_branches") is True and policy.get("custom_branch_policies") is False
    record(
        "environment_protected_branches_only",
        branch_policy_ok,
        {"protected_branches_only": branch_policy_ok},
    )
    reviewer_rules: list[Mapping[str, Any]] = []
    for rule_value in rules:
        rule = _mapping(rule_value, "github_readiness_environment")
        if rule.get("type") == "required_reviewers":
            reviewer_rules.append(rule)
    reviewer_count = 0
    prevent_self_review = False
    if len(reviewer_rules) == 1:
        reviewers = _array(
            reviewer_rules[0].get("reviewers"),
            "github_readiness_environment",
        )
        reviewer_count = len(reviewers)
        prevent_self_review = reviewer_rules[0].get("prevent_self_review") is True
    record(
        "independent_environment_approval",
        len(reviewer_rules) == 1 and reviewer_count >= 1 and prevent_self_review,
        {
            "prevent_self_review": prevent_self_review,
            "reviewer_count": reviewer_count,
        },
    )

    secrets_payload = _mapping(
        snapshot.get("environment_secrets"),
        "github_readiness_api_schema",
    )
    secret_values = _bounded_collection(secrets_payload, "secrets")
    secret_names: set[str] = set()
    for secret_value in secret_values:
        secret = _mapping(secret_value, "github_readiness_api_schema")
        name = secret.get("name")
        if type(name) is not str or _SECRET_RE.fullmatch(name) is None or name in secret_names:
            _reject("github_readiness_secret")
        secret_names.add(name)
    record(
        "minimal_environment_secret_inventory",
        secret_names == REQUIRED_SECRETS,
        {
            "expected_count": len(REQUIRED_SECRETS),
            "observed_count": len(secret_names),
        },
    )

    runner_group_value = snapshot.get("runner_group")
    runner_group = None if runner_group_value is None else _mapping(runner_group_value, "github_readiness_runner_group")
    selected_workflows: list[Any] = []
    if runner_group is not None:
        selected_workflows_value = runner_group.get("selected_workflows")
        if selected_workflows_value is not None:
            selected_workflows = _array(
                selected_workflows_value,
                "github_readiness_runner_group",
            )
        if any(type(value) is not str for value in selected_workflows) or len(set(selected_workflows)) != len(
            selected_workflows
        ):
            _reject("github_readiness_runner_group")
    group_repositories_payload = _mapping(
        snapshot.get("runner_group_repositories"),
        "github_readiness_runner_group",
    )
    group_repository_values = _bounded_collection(
        group_repositories_payload,
        "repositories",
    )
    group_repositories: set[tuple[int, str]] = set()
    for group_repository_value in group_repository_values:
        group_repository = _mapping(
            group_repository_value,
            "github_readiness_runner_group",
        )
        full_name = group_repository.get("full_name")
        repository_id = group_repository.get("id")
        if (
            type(repository_id) is not int
            or repository_id < 1
            or type(full_name) is not str
            or _REPOSITORY_RE.fullmatch(full_name) is None
        ):
            _reject("github_readiness_runner_group")
        identity = (repository_id, full_name)
        if identity in group_repositories:
            _reject("github_readiness_runner_group")
        group_repositories.add(identity)
    selected_workflow = f"{repository}/{WORKFLOW_PATH}@refs/heads/{default_branch}"
    runner_group_ready = (
        repository_identity_matches
        and owner_is_organization
        and runner_group is not None
        and runner_group.get("name") == RUNNER_GROUP_NAME
        and runner_group.get("default") is False
        and runner_group.get("visibility") == "selected"
        and runner_group.get("allows_public_repositories") is True
        and runner_group.get("restricted_to_workflows") is True
        and selected_workflows == [selected_workflow]
        and group_repositories == {(EXPECTED_REPOSITORY_ID, repository)}
    )
    record(
        "workflow_restricted_signing_runner_group",
        runner_group_ready,
        {
            "expected_repository_count": 1,
            "expected_workflow_count": 1,
            "group_present": runner_group is not None,
            "repository_count": len(group_repositories),
            "workflow_count": len(selected_workflows),
        },
    )

    runners_payload = _mapping(snapshot.get("runners"), "github_readiness_api_schema")
    runner_values = _bounded_collection(runners_payload, "runners")
    candidates = 0
    eligible = 0
    legacy = 0
    for runner_value in runner_values:
        runner = _mapping(runner_value, "github_readiness_runner")
        label_values = _array(runner.get("labels"), "github_readiness_runner")
        labels: set[str] = set()
        for label_value in label_values:
            label = _mapping(label_value, "github_readiness_runner").get("name")
            if type(label) is not str or not label or label in labels:
                _reject("github_readiness_runner")
            labels.add(label)
        if LEGACY_RUNNER_LABEL in labels:
            legacy += 1
        if EPHEMERAL_RUNNER_LABEL not in labels:
            continue
        candidates += 1
        if (
            labels == REQUIRED_RUNNER_LABELS
            and runner.get("ephemeral") is True
            and runner.get("status") == "online"
            and runner.get("busy") is False
        ):
            eligible += 1
    runner_ready = candidates == 1 and eligible == 1 and legacy == 0
    record(
        "single_ephemeral_signing_runner",
        runner_ready,
        {
            "candidate_count": candidates,
            "eligible_count": eligible,
            "legacy_label_count": legacy,
        },
    )

    workflows_payload = _mapping(snapshot.get("workflows"), "github_readiness_api_schema")
    workflow_values = _bounded_collection(workflows_payload, "workflows")
    matching_workflows = []
    registered_source_paths: set[str] = set()
    active_source_paths: set[str] = set()
    platform_paths: set[str] = set()
    unknown_platform_paths: set[str] = set()
    for workflow_value in workflow_values:
        workflow = _mapping(workflow_value, "github_readiness_api_schema")
        path = workflow.get("path")
        if (
            type(path) is not str
            or _REGISTERED_WORKFLOW_PATH_RE.fullmatch(path) is None
            or ".." in path
            or "//" in path
            or path.endswith("/")
        ):
            _reject("github_readiness_workflow")
        if _WORKFLOW_PATH_RE.fullmatch(path) is not None:
            if path in registered_source_paths:
                _reject("github_readiness_workflow")
            registered_source_paths.add(path)
            if workflow.get("state") == "active":
                active_source_paths.add(path)
            if path == WORKFLOW_PATH:
                matching_workflows.append(workflow)
        else:
            if path in platform_paths or path in unknown_platform_paths:
                _reject("github_readiness_workflow")
            if path in _PLATFORM_WORKFLOW_PATHS:
                platform_paths.add(path)
            else:
                unknown_platform_paths.add(path)
    registered = len(matching_workflows) == 1 and matching_workflows[0].get("state") == "active"
    record(
        "signing_workflow_registered",
        registered,
        {"active_exact_path_count": 1 if registered else 0},
    )
    remote_files_value = _mapping(
        snapshot.get("remote_workflow_files"),
        "github_readiness_workflow",
    )
    if not 1 <= len(remote_files_value) <= 100:
        _reject("github_readiness_workflow")
    remote_workflows: dict[str, bytes] = {}
    for path, value in remote_files_value.items():
        if type(path) is not str or path in remote_workflows:
            _reject("github_readiness_workflow")
        remote_workflows[path] = _remote_workflow_payload(value, expected_path=path)
    remote_workflow = remote_workflows.get(WORKFLOW_PATH)
    remote_digest = "" if remote_workflow is None else "sha256:" + hashlib.sha256(remote_workflow).hexdigest()
    source_matches = remote_digest == local_digest
    record(
        "signing_workflow_source_identity",
        source_matches,
        {"matches_local_digest": source_matches},
    )
    inventory_matches = set(remote_workflows) == set(local_workflows) and all(
        hashlib.sha256(remote_workflows[path]).digest() == hashlib.sha256(local_workflows[path]).digest()
        for path in local_workflows
        if path in remote_workflows
    )
    record(
        "workflow_source_inventory_identity",
        inventory_matches,
        {
            "expected_count": len(local_workflows),
            "observed_count": len(remote_workflows),
        },
    )
    registry_matches = registered_source_paths == set(local_workflows) and active_source_paths == set(local_workflows)
    record(
        "workflow_registry_source_identity",
        registry_matches,
        {
            "active_count": len(active_source_paths),
            "expected_count": len(local_workflows),
            "registered_count": len(registered_source_paths),
        },
    )
    platform_inventory_ready = not unknown_platform_paths
    record(
        "platform_workflow_inventory",
        platform_inventory_ready,
        {
            "recognized_count": len(platform_paths),
            "unrecognized_count": len(unknown_platform_paths),
        },
    )
    contract_passes = remote_workflow is not None and _signing_workflow_contract(remote_workflow)
    record(
        "protected_signing_trust_contract",
        contract_passes,
        {
            "contract_valid": contract_passes,
            "remote_workflow_present": remote_workflow is not None,
        },
    )
    exclusive_authority = _exclusive_signing_authority(remote_workflows)
    record(
        "exclusive_signing_workflow_authority",
        exclusive_authority,
        {"exclusive": exclusive_authority},
    )

    blocked_count = sum(check["status"] == "blocked" for check in checks)
    return {
        "blocked_count": blocked_count,
        "checks": checks,
        "default_branch": default_branch,
        "expected_workflow_digest": local_digest,
        "limitations": (
            "Read-only GitHub control-plane readiness only. It requires an organization "
            "runner group restricted to the exact protected workflow, but does not prove "
            "runner image cleanliness, certificate custody, Keychain state, signing, "
            "notarization, Gatekeeper, installation, TCC, browser pairing, log receipt, "
            "runner destruction, or artifact correctness."
        ),
        "passed_count": len(checks) - blocked_count,
        "public_claim_eligible": False,
        "repository": repository,
        "repository_visibility": visibility,
        "schema_version": 1,
        "status": "passed" if blocked_count == 0 else "blocked",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="Seabass-up/Algo-cli")
    arguments = parser.parse_args(argv)
    gh_binary = shutil.which("gh")
    if gh_binary is None:
        print(
            json.dumps(
                {"reason_code": "github_readiness_gh_missing", "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3
    try:
        snapshot = collect_snapshot(repository=arguments.repository, gh_binary=gh_binary)
        report = assess_snapshot(
            snapshot,
            repository=arguments.repository,
            expected_workflow=ROOT / WORKFLOW_PATH,
        )
    except GitHubHardeningReadinessRejected as error:
        print(
            json.dumps(
                {"reason_code": error.reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    report["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
