#!/usr/bin/env python3
"""Fail-closed authority checks for the draft-first public release process."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, NoReturn
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Seabass-up/Algo-cli"
REPOSITORY_ID = 1_297_752_684
DEFAULT_BRANCH = "main"
DEFAULT_REF = "refs/heads/main"
CI_WORKFLOW_PATH = ".github/workflows/oliver-ci.yml"
CI_WORKFLOW_REF = f"{REPOSITORY}/{CI_WORKFLOW_PATH}@{DEFAULT_REF}"
CI_WORKFLOW_IDENTITY = f"https://github.com/{CI_WORKFLOW_REF}"
RELEASE_WORKFLOW_PATH = ".github/workflows/oliver-release.yml"
RELEASE_WORKFLOW_REF = f"{REPOSITORY}/{RELEASE_WORKFLOW_PATH}@{DEFAULT_REF}"
RELEASE_WORKFLOW_IDENTITY = f"https://github.com/{RELEASE_WORKFLOW_REF}"
BORON_ARTIFACT_NAME = "boron-browser-boundary"
BORON_REPORT_NAME = "grace-boron-hosted-qualification.json"
RELEASE_TAG_RULESET_PATTERN = "refs/tags/v*"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
CYCLONEDX_PREDICATE = "https://cyclonedx.org/bom"
BORON_RELEASE_PREDICATE = "https://algo-cli.com/attestations/boron-hosted-qualification/v1"
SOURCE_BINDING_PREDICATE = "https://algo-cli.com/attestations/release-source-binding/v1"
IN_TOTO_STATEMENT_V1 = "https://in-toto.io/Statement/v1"
API_VERSION = "2026-03-10"
MAX_API_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ATTESTATION_BYTES = 32 * 1024 * 1024
MAX_GITHUB_OUTPUT_BYTES = 1024 * 1024
ATTESTATION_CLOCK_SKEW = timedelta(minutes=10)

SIGSTORE_BUNDLE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.dev.sigstore.bundle.v0.3+json",
        "application/vnd.dev.sigstore.bundle+json;version=0.3",
    }
)
SIGSTORE_VERIFICATION_RESULT_MEDIA_TYPE = "application/vnd.dev.sigstore.verificationresult+json;version=0.1"
DSSE_IN_TOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]{0,15})$")
_GITHUB_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_RFC3339_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class ReleaseAuthorityRejected(RuntimeError):
    """A content-free release authority rejection."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _reject(reason_code: str) -> NoReturn:
    raise ReleaseAuthorityRejected(reason_code)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("release_json_duplicate_key")
        result[key] = value
    return result


def _json_bytes(payload: bytes, *, maximum: int, reason_code: str) -> Any:
    if type(payload) is not bytes or not 1 <= len(payload) <= maximum:
        _reject(reason_code)
    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: _reject(reason_code),
        )
    except (UnicodeError, json.JSONDecodeError):
        _reject(reason_code)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _reject("release_json_shape")


def _positive_integer(value: Any, reason_code: str) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 53) - 1:
        _reject(reason_code)
    return value


def _revision(value: Any, reason_code: str) -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        _reject(reason_code)
    return value


def _digest(value: Any, reason_code: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _reject(reason_code)
    return value


def _timestamp(value: Any, *, github: bool, reason_code: str) -> datetime:
    pattern = _GITHUB_TIMESTAMP_RE if github else _RFC3339_TIMESTAMP_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject(reason_code)
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _reject(reason_code)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        _reject(reason_code)


def _https_uri(value: Any, reason_code: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048:
        _reject(reason_code)
    try:
        parsed = urlsplit(value)
    except ValueError:
        _reject(reason_code)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _reject(reason_code)
    return value


def _tag(value: Any) -> str:
    if type(value) is not str or _TAG_RE.fullmatch(value) is None:
        _reject("release_tag")
    return value


def _version(tag: str) -> str:
    return _tag(tag)[1:]


def expected_release_assets(tag: str) -> frozenset[str]:
    version = _version(tag)
    return frozenset(
        {
            f"algo_cli_runtime-{version}-py3-none-any.whl",
            f"algo_cli_runtime-{version}.tar.gz",
            "algo-cli-runtime.lock.cdx.json",
            "SHA256SUMS",
            BORON_REPORT_NAME,
            "grace-boron-hosted-qualification.sigstore.jsonl",
            "algo-cli-release-provenance.sigstore.jsonl",
            "algo-cli-release-sbom.sigstore.jsonl",
            "grace-boron-release-qualification.sigstore.jsonl",
            "oliver-release-source-binding.sigstore.jsonl",
            "oliver-release-authority.json",
            "oliver-release-source.tar",
            "oliver-release-source-receipt.json",
            "oliver-release-source-binding.json",
            "oliver-release-repository-policy.json",
            "algo-cli-release-auditable-requirements.txt",
            "grace-release-evidence-SHA256SUMS",
        }
    )


def _inner_evidence_names(tag: str) -> frozenset[str]:
    version = _version(tag)
    return frozenset(
        {
            f"algo_cli_runtime-{version}-py3-none-any.whl",
            f"algo_cli_runtime-{version}.tar.gz",
            "algo-cli-runtime.lock.cdx.json",
        }
    )


def _validate_asset_checksums(
    payloads: Mapping[str, bytes],
    *,
    tag: str,
    outer_reason: str,
) -> None:
    expected = expected_release_assets(tag)
    if set(payloads) != expected:
        _reject("release_asset_closure_set")
    inner_names = _inner_evidence_names(tag)
    inner_rows = "".join(
        f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n" for name in sorted(inner_names)
    ).encode("ascii")
    if payloads["SHA256SUMS"] != inner_rows:
        _reject("release_evidence_checksum")
    outer_name = "grace-release-evidence-SHA256SUMS"
    outer_rows = "".join(
        f"{hashlib.sha256(payloads[name]).hexdigest()}  {name}\n" for name in sorted(expected - {outer_name})
    ).encode("ascii")
    if payloads[outer_name] != outer_rows:
        _reject(outer_reason)


def validate_release_asset_closure(*, tag: str, directory: Path) -> dict[str, Any]:
    """Validate exact release names, regular bytes, and inner/outer checksum closure."""

    expected = expected_release_assets(tag)
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _reject("release_asset_closure_directory")
    if len(entries) != len(expected) or {entry.name for entry in entries} != expected:
        _reject("release_asset_closure_set")
    payloads = {
        entry.name: _read_regular(
            entry,
            maximum=MAX_ARTIFACT_BYTES,
            reason_code="release_asset_closure_file",
        )
        for entry in entries
    }
    _validate_asset_checksums(payloads, tag=tag, outer_reason="release_asset_outer_checksum")
    return {
        "schema_version": 1,
        "kind": "algo-cli-release-asset-closure",
        "status": "passed",
        "tag": _tag(tag),
    }


def boron_artifact_name(run_attempt: int) -> str:
    """Return the immutable attempt-scoped hosted Boron artifact name."""

    return f"{BORON_ARTIFACT_NAME}-attempt-{_positive_integer(run_attempt, 'release_ci_run_attempt')}"


def validate_repository_policy(
    *,
    immutable: Any,
    summaries: Any,
    details: Mapping[int, Any],
) -> dict[str, Any]:
    """Validate immutable releases and one exact, active, no-bypass v* tag ruleset."""

    immutable_document = _exact_mapping(immutable, {"enabled"}, "release_immutability_authority")
    if immutable_document["enabled"] is not True:
        _reject("release_immutability_authority")
    if type(summaries) is not list or not 1 <= len(summaries) < 100 or type(details) is not dict:
        _reject("release_repository_policy")

    summary_ids: set[int] = set()
    tag_rulesets: list[dict[str, Any]] = []
    for raw_summary in summaries:
        summary = _exact_mapping(
            raw_summary,
            {"id", "name", "source", "enforcement", "target"},
            "release_repository_policy",
        )
        ruleset_id = _positive_integer(summary["id"], "release_repository_policy")
        if ruleset_id in summary_ids:
            _reject("release_repository_policy")
        summary_ids.add(ruleset_id)
        if ruleset_id not in details:
            _reject("release_repository_policy")
        detail = _exact_mapping(
            details[ruleset_id],
            {
                "id",
                "name",
                "source",
                "source_type",
                "enforcement",
                "target",
                "bypass_actors",
                "conditions",
                "rules",
            },
            "release_repository_policy",
        )
        if any(detail[key] != summary[key] for key in ("id", "name", "source", "enforcement", "target")):
            _reject("release_repository_policy")
        if detail["target"] == "tag":
            tag_rulesets.append(detail)
    if set(details) != summary_ids or len(tag_rulesets) != 1:
        _reject("release_repository_policy")

    ruleset = tag_rulesets[0]
    if (
        ruleset["source_type"] != "Repository"
        or ruleset["source"] != REPOSITORY
        or ruleset["enforcement"] != "active"
        or ruleset["bypass_actors"] != []
    ):
        _reject("release_repository_policy")
    conditions = _exact_mapping(ruleset["conditions"], {"ref_name"}, "release_repository_policy")
    if set(conditions) != {"ref_name"}:
        _reject("release_repository_policy")
    ref_name = _exact_mapping(conditions["ref_name"], {"include", "exclude"}, "release_repository_policy")
    if set(ref_name) != {"include", "exclude"} or ref_name != {
        "exclude": [],
        "include": [RELEASE_TAG_RULESET_PATTERN],
    }:
        _reject("release_repository_policy")
    rules = ruleset["rules"]
    if type(rules) is not list or len(rules) != 2:
        _reject("release_repository_policy")
    rule_types: set[str] = set()
    for raw_rule in rules:
        if type(raw_rule) is not dict or raw_rule.get("type") not in {"update", "deletion"}:
            _reject("release_repository_policy")
        if raw_rule["type"] == "deletion" and set(raw_rule) != {"type"}:
            _reject("release_repository_policy")
        if raw_rule["type"] == "update" and not (
            set(raw_rule) == {"type"}
            or (
                set(raw_rule) == {"type", "parameters"}
                and raw_rule["parameters"] == {"update_allows_fetch_and_merge": False}
            )
        ):
            _reject("release_repository_policy")
        if raw_rule["type"] in rule_types:
            _reject("release_repository_policy")
        rule_types.add(raw_rule["type"])
    if rule_types != {"update", "deletion"}:
        _reject("release_repository_policy")

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "repository": REPOSITORY,
        "immutable_releases": True,
        "tag_ruleset": {
            "bypass_actors": [],
            "enforcement": "active",
            "id": ruleset["id"],
            "pattern": RELEASE_TAG_RULESET_PATTERN,
            "rules": ["deletion", "update"],
            "target": "tag",
        },
    }
    receipt["policy_digest"] = "sha256:" + hashlib.sha256(_canonical(receipt)).hexdigest()
    return receipt


def _validate_policy_receipt(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "status",
        "repository",
        "immutable_releases",
        "tag_ruleset",
        "policy_digest",
    }:
        _reject("release_repository_policy_receipt")
    unsigned = dict(value)
    digest = unsigned.pop("policy_digest")
    ruleset = _exact_mapping(
        value["tag_ruleset"],
        {"bypass_actors", "enforcement", "id", "pattern", "rules", "target"},
        "release_repository_policy_receipt",
    )
    if (
        set(ruleset) != {"bypass_actors", "enforcement", "id", "pattern", "rules", "target"}
        or value["schema_version"] != 1
        or value["status"] != "passed"
        or value["repository"] != REPOSITORY
        or value["immutable_releases"] is not True
        or ruleset["bypass_actors"] != []
        or ruleset["enforcement"] != "active"
        or _positive_integer(ruleset["id"], "release_repository_policy_receipt") < 1
        or ruleset["pattern"] != RELEASE_TAG_RULESET_PATTERN
        or ruleset["rules"] != ["deletion", "update"]
        or ruleset["target"] != "tag"
        or digest != "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    ):
        _reject("release_repository_policy_receipt")
    return value


@dataclass(frozen=True, slots=True)
class DispatchContext:
    source_revision: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "DispatchContext":
        if type(environment) is not dict or environment.get("GITHUB_ACTIONS") != "true":
            _reject("release_environment")
        if environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            _reject("release_event")
        if environment.get("GITHUB_REPOSITORY") != REPOSITORY or environment.get("GITHUB_REPOSITORY_ID") != str(
            REPOSITORY_ID
        ):
            _reject("release_repository")
        if environment.get("GITHUB_REF") != DEFAULT_REF or environment.get("GITHUB_REF_PROTECTED") != "true":
            _reject("release_dispatch_ref")
        revision = _revision(environment.get("GITHUB_SHA"), "release_dispatch_revision")
        if environment.get("GITHUB_WORKFLOW_SHA") != revision:
            _reject("release_workflow_revision")
        workflow_ref = environment.get("GITHUB_WORKFLOW_REF")
        if workflow_ref != f"{REPOSITORY}/.github/workflows/oliver-release.yml@{DEFAULT_REF}":
            _reject("release_workflow_ref")
        if (
            environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or environment.get("RUNNER_OS") != "Linux"
            or environment.get("RUNNER_ARCH") != "X64"
        ):
            _reject("release_runner")
        return cls(source_revision=revision)


ApiGet = Callable[[str], Any]


def _exact_mapping(value: Any, required: set[str], reason_code: str) -> dict[str, Any]:
    if type(value) is not dict or not required <= set(value):
        _reject(reason_code)
    return value


def _resolve_tag(api_get: ApiGet, tag: str) -> str:
    reference = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/git/ref/tags/{quote(tag, safe='')}"),
        {"ref", "object"},
        "release_tag_reference",
    )
    if reference["ref"] != f"refs/tags/{tag}":
        _reject("release_tag_reference")
    raw_object = reference["object"]
    seen: set[str] = set()
    for _depth in range(8):
        obj = _exact_mapping(raw_object, {"type", "sha"}, "release_tag_object")
        sha = _revision(obj["sha"], "release_tag_revision")
        object_type = obj["type"]
        if object_type == "commit":
            return sha
        if object_type != "tag" or sha in seen:
            _reject("release_tag_object")
        seen.add(sha)
        tag_object = _exact_mapping(
            api_get(f"repos/{REPOSITORY}/git/tags/{sha}"),
            {"sha", "object"},
            "release_annotated_tag",
        )
        if tag_object["sha"] != sha:
            _reject("release_annotated_tag")
        raw_object = tag_object["object"]
    _reject("release_tag_depth")


def _existing_assets(value: Any, *, tag: str) -> list[dict[str, Any]]:
    if type(value) is not list:
        _reject("release_assets")
    allowed = expected_release_assets(tag)
    observed: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in value:
        asset = _exact_mapping(
            raw,
            {"id", "name", "state", "size", "digest"},
            "release_asset",
        )
        name = asset["name"]
        if type(name) is not str or name not in allowed or name in observed:
            _reject("release_asset_name")
        observed.add(name)
        size = _positive_integer(asset["size"], "release_asset_size")
        normalized.append(
            {
                "digest": _digest(asset["digest"], "release_asset_digest"),
                "id": _positive_integer(asset["id"], "release_asset_id"),
                "name": name,
                "size": size,
                "state": "uploaded",
            }
        )
        if asset["state"] != "uploaded":
            _reject("release_asset_state")
    return sorted(normalized, key=lambda row: row["name"])


def durable_asset_plan(
    *,
    tag: str,
    release_id: int,
    release_state: str,
    release: Any,
) -> dict[str, Any]:
    """Validate and normalize the exact durable assets on a draft or immutable release."""

    release_tag = _tag(tag)
    expected_id = _positive_integer(release_id, "release_id")
    if release_state not in {"draft-exact", "published-exact"}:
        _reject("release_reconcile_state")
    document = _exact_mapping(
        release,
        {"id", "tag_name", "draft", "prerelease", "immutable", "published_at", "assets"},
        "release_reconcile_release",
    )
    if document["id"] != expected_id or document["tag_name"] != release_tag:
        _reject("release_reconcile_release")
    if release_state == "draft-exact":
        valid_state = (
            document["draft"] is True
            and document["prerelease"] is False
            and document["immutable"] is False
            and document["published_at"] is None
        )
    else:
        valid_state = (
            document["draft"] is False
            and document["prerelease"] is False
            and document["immutable"] is True
            and document["published_at"] is not None
        )
        if valid_state:
            _timestamp(document["published_at"], github=True, reason_code="release_published_time")
    if not valid_state:
        _reject("release_reconcile_state")
    assets = _existing_assets(document["assets"], tag=release_tag)
    if {row["name"] for row in assets} != expected_release_assets(release_tag):
        _reject("release_reconcile_asset_set")
    if any(row["size"] > MAX_ARTIFACT_BYTES for row in assets):
        _reject("release_reconcile_asset_size")
    return {
        "schema_version": 1,
        "kind": "algo-cli-release-asset-plan",
        "release": {"id": expected_id, "state": release_state, "tag": release_tag},
        "assets": assets,
    }


def validate_durable_assets(
    *,
    tag: str,
    release_id: int,
    release_state: str,
    source_revision: str,
    release: Any,
    directory: Path,
    authority_path: Path,
    policy_path: Path,
    report_path: Path,
    boron_bundle_path: Path,
) -> dict[str, Any]:
    """Bind downloaded release bytes to API digests, checksums, and stable authority."""

    source = _revision(source_revision, "release_reconcile_source")
    plan = durable_asset_plan(
        tag=tag,
        release_id=release_id,
        release_state=release_state,
        release=release,
    )
    expected = expected_release_assets(tag)
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _reject("release_reconcile_directory")
    if len(entries) != len(expected) or {entry.name for entry in entries} != expected:
        _reject("release_reconcile_asset_set")

    payloads: dict[str, bytes] = {}
    metadata = {row["name"]: row for row in plan["assets"]}
    for entry in entries:
        payload = _read_regular(
            entry,
            maximum=MAX_ARTIFACT_BYTES,
            reason_code="release_reconcile_asset_file",
        )
        row = metadata[entry.name]
        if len(payload) != row["size"] or "sha256:" + hashlib.sha256(payload).hexdigest() != row["digest"]:
            _reject("release_reconcile_asset_digest")
        payloads[entry.name] = payload

    _validate_asset_checksums(payloads, tag=tag, outer_reason="release_reconcile_checksum")

    current_authority = _read_regular(
        authority_path,
        maximum=MAX_API_BYTES,
        reason_code="release_reconcile_authority_file",
    )
    current_policy = _read_regular(
        policy_path,
        maximum=MAX_API_BYTES,
        reason_code="release_reconcile_policy_file",
    )
    if payloads["oliver-release-authority.json"] != current_authority:
        _reject("release_reconcile_authority")
    if payloads["oliver-release-repository-policy.json"] != current_policy:
        _reject("release_reconcile_policy")
    current_report = _read_regular(
        report_path,
        maximum=MAX_REPORT_BYTES,
        reason_code="release_reconcile_report_file",
    )
    current_boron_bundle = _read_regular(
        boron_bundle_path,
        maximum=MAX_ATTESTATION_BYTES,
        reason_code="release_reconcile_boron_bundle_file",
    )
    if payloads[BORON_REPORT_NAME] != current_report:
        _reject("release_reconcile_report")
    if payloads["grace-boron-hosted-qualification.sigstore.jsonl"] != current_boron_bundle:
        _reject("release_reconcile_boron_bundle")
    authority = _load_authority(directory / "oliver-release-authority.json")
    policy = _load_policy_receipt(directory / "oliver-release-repository-policy.json")
    if (
        authority["release"] != {"id": release_id, "tag": tag}
        or authority["source"]["revision"] != source
        or authority["policy"]
        != {
            "digest": policy["policy_digest"],
            "ruleset_id": policy["tag_ruleset"]["id"],
        }
    ):
        _reject("release_reconcile_authority")
    return {
        "schema_version": 1,
        "kind": "algo-cli-durable-release-reconciliation",
        "status": "passed",
        "release": plan["release"],
        "source_revision": authority["source"]["revision"],
    }


def _require_ancestor(api_get: ApiGet, *, source_revision: str, head_revision: str) -> None:
    comparison = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/compare/{source_revision}...{head_revision}"),
        {"status", "ahead_by", "behind_by", "base_commit", "merge_base_commit"},
        "release_source_ancestry",
    )
    base = _exact_mapping(comparison["base_commit"], {"sha"}, "release_source_ancestry")
    merge_base = _exact_mapping(comparison["merge_base_commit"], {"sha"}, "release_source_ancestry")
    if (
        comparison["status"] != "ahead"
        or type(comparison["ahead_by"]) is not int
        or comparison["ahead_by"] < 1
        or comparison["behind_by"] != 0
        or base["sha"] != source_revision
        or merge_base["sha"] != source_revision
    ):
        _reject("release_source_ancestry")


def validate_authority(
    *,
    tag: str,
    environment: Mapping[str, str],
    checkout_revision: str,
    policy_receipt: Mapping[str, Any],
    api_get: ApiGet,
) -> tuple[dict[str, Any], str]:
    """Bind one draft or already-published release to protected-main Boron CI."""

    release_tag = _tag(tag)
    context = DispatchContext.from_environment(environment)
    validated_policy = _validate_policy_receipt(policy_receipt)
    checkout = _revision(checkout_revision, "release_checkout_revision")
    if checkout != context.source_revision:
        _reject("release_checkout_revision")

    repository = _exact_mapping(
        api_get(f"repos/{REPOSITORY}"),
        {"id", "full_name", "default_branch", "archived", "fork"},
        "release_repository_metadata",
    )
    if (
        repository["id"] != REPOSITORY_ID
        or repository["full_name"] != REPOSITORY
        or repository["default_branch"] != DEFAULT_BRANCH
        or repository["archived"] is not False
        or repository["fork"] is not False
    ):
        _reject("release_repository_metadata")

    branch = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/branches/{DEFAULT_BRANCH}"),
        {"name", "protected", "commit"},
        "release_default_branch",
    )
    branch_commit = _exact_mapping(branch["commit"], {"sha"}, "release_default_branch")
    branch_revision = _revision(branch_commit["sha"], "release_default_branch_revision")
    if (
        branch["name"] != DEFAULT_BRANCH
        or branch["protected"] is not True
        or branch_revision != context.source_revision
    ):
        _reject("release_default_branch")

    tag_revision = _resolve_tag(api_get, release_tag)

    release = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/releases/tags/{quote(release_tag, safe='')}"),
        {
            "id",
            "tag_name",
            "target_commitish",
            "draft",
            "prerelease",
            "immutable",
            "published_at",
            "assets",
        },
        "release_draft",
    )
    if release["tag_name"] != release_tag or release["target_commitish"] not in {
        DEFAULT_BRANCH,
        branch_revision,
        tag_revision,
    }:
        _reject("release_draft")
    release_id = _positive_integer(release["id"], "release_id")
    assets = _existing_assets(release["assets"], tag=release_tag)
    expected_names = expected_release_assets(release_tag)
    observed_names = {row["name"] for row in assets}
    if (
        release["draft"] is True
        and release["prerelease"] is False
        and release["immutable"] is False
        and release["published_at"] is None
    ):
        if not observed_names:
            release_state = "draft"
        elif observed_names == expected_names:
            release_state = "draft-exact"
        else:
            _reject("release_draft_asset_set")
    elif (
        release["draft"] is False
        and release["prerelease"] is False
        and release["immutable"] is True
        and release["published_at"] is not None
        and observed_names == expected_names
    ):
        _timestamp(release["published_at"], github=True, reason_code="release_published_time")
        release_state = "published-exact"
    else:
        _reject("release_draft")
    if tag_revision != branch_revision:
        if not assets:
            _reject("release_tag_not_default_head")
        _require_ancestor(api_get, source_revision=tag_revision, head_revision=branch_revision)

    workflow = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/actions/workflows/{CI_WORKFLOW_PATH}"),
        {"id", "name", "path", "state"},
        "release_ci_workflow",
    )
    workflow_id = _positive_integer(workflow["id"], "release_ci_workflow_id")
    if workflow["name"] != "CI" or workflow["path"] != CI_WORKFLOW_PATH or workflow["state"] != "active":
        _reject("release_ci_workflow")

    query = (
        f"repos/{REPOSITORY}/actions/workflows/{CI_WORKFLOW_PATH}/runs"
        f"?branch={DEFAULT_BRANCH}&event=push&head_sha={tag_revision}"
        "&status=success&per_page=100"
    )
    runs_document = _exact_mapping(
        api_get(query),
        {"total_count", "workflow_runs"},
        "release_ci_runs",
    )
    runs = runs_document["workflow_runs"]
    if runs_document["total_count"] != 1 or type(runs) is not list or len(runs) != 1:
        _reject("release_ci_run_count")
    run = _exact_mapping(
        runs[0],
        {
            "id",
            "run_attempt",
            "workflow_id",
            "path",
            "head_branch",
            "head_sha",
            "event",
            "status",
            "conclusion",
            "run_started_at",
            "updated_at",
            "repository",
            "head_repository",
        },
        "release_ci_run",
    )
    run_repository = _exact_mapping(run["repository"], {"id", "full_name"}, "release_ci_run_repository")
    head_repository = _exact_mapping(run["head_repository"], {"id", "full_name"}, "release_ci_run_repository")
    run_id = _positive_integer(run["id"], "release_ci_run_id")
    run_attempt = _positive_integer(run["run_attempt"], "release_ci_run_attempt")
    run_started_at = _timestamp(run["run_started_at"], github=True, reason_code="release_ci_run_time")
    run_completed_at = _timestamp(run["updated_at"], github=True, reason_code="release_ci_run_time")
    if (
        run["workflow_id"] != workflow_id
        or run["path"] != CI_WORKFLOW_PATH
        or run["head_branch"] != DEFAULT_BRANCH
        or run["head_sha"] != tag_revision
        or run["event"] != "push"
        or run["status"] != "completed"
        or run["conclusion"] != "success"
        or run_completed_at < run_started_at
        or run_repository != {"id": REPOSITORY_ID, "full_name": REPOSITORY}
        or head_repository != {"id": REPOSITORY_ID, "full_name": REPOSITORY}
    ):
        _reject("release_ci_run")

    artifact_name = boron_artifact_name(run_attempt)
    artifacts_document = _exact_mapping(
        api_get(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?name={artifact_name}&per_page=100"),
        {"total_count", "artifacts"},
        "release_boron_artifacts",
    )
    artifacts = artifacts_document["artifacts"]
    if artifacts_document["total_count"] != 1 or type(artifacts) is not list or len(artifacts) != 1:
        _reject("release_boron_artifact_count")
    artifact = _exact_mapping(
        artifacts[0],
        {
            "id",
            "name",
            "expired",
            "size_in_bytes",
            "digest",
            "workflow_run",
        },
        "release_boron_artifact",
    )
    artifact_run = _exact_mapping(
        artifact["workflow_run"],
        {"id", "repository_id", "head_repository_id", "head_branch", "head_sha"},
        "release_boron_artifact_run",
    )
    artifact_size = _positive_integer(artifact["size_in_bytes"], "release_boron_artifact_size")
    if (
        artifact["name"] != artifact_name
        or artifact["expired"] is not False
        or artifact_size > MAX_ARTIFACT_BYTES
        or artifact_run
        != {
            "head_branch": DEFAULT_BRANCH,
            "head_repository_id": REPOSITORY_ID,
            "head_sha": tag_revision,
            "id": run_id,
            "repository_id": REPOSITORY_ID,
        }
    ):
        _reject("release_boron_artifact")

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "repository": {
            "default_branch": DEFAULT_BRANCH,
            "id": REPOSITORY_ID,
            "name": REPOSITORY,
        },
        "policy": {
            "digest": validated_policy["policy_digest"],
            "ruleset_id": validated_policy["tag_ruleset"]["id"],
        },
        "release": {
            "id": release_id,
            "tag": release_tag,
        },
        "source": {
            "protected": True,
            "ref": DEFAULT_REF,
            "revision": tag_revision,
        },
        "boron": {
            "artifact_digest": _digest(artifact["digest"], "release_boron_artifact_digest"),
            "artifact_id": _positive_integer(artifact["id"], "release_boron_artifact_id"),
            "artifact_size": artifact_size,
            "run_attempt": run_attempt,
            "run_completed_at": run["updated_at"],
            "run_id": run_id,
            "run_started_at": run["run_started_at"],
            "workflow_id": workflow_id,
            "workflow_path": CI_WORKFLOW_PATH,
        },
    }
    receipt["authority_digest"] = "sha256:" + hashlib.sha256(_canonical(receipt)).hexdigest()
    return receipt, release_state


def _validate_authority_receipt(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "status",
        "repository",
        "policy",
        "release",
        "source",
        "boron",
        "authority_digest",
    }:
        _reject("release_authority_receipt")
    digest = value["authority_digest"]
    unsigned = dict(value)
    del unsigned["authority_digest"]
    if (
        value["schema_version"] != 1
        or value["status"] != "passed"
        or digest != "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()
    ):
        _reject("release_authority_receipt")
    return value


def _stable_file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _same_file_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode), left.st_nlink) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_nlink,
    )


def _read_regular(path: Path, *, maximum: int, reason_code: str) -> bytes:
    """Read one immutable regular-file identity without following a swapped link."""

    descriptor = -1
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 1 <= before.st_size <= maximum:
            _reject(reason_code)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_file_identity(opened) != _stable_file_identity(before):
            _reject(reason_code)
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                _reject(reason_code)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject(reason_code)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _stable_file_identity(after) != _stable_file_identity(opened) or _stable_file_identity(
            current
        ) != _stable_file_identity(opened):
            _reject(reason_code)
        payload = b"".join(chunks)
        if len(payload) != opened.st_size:
            _reject(reason_code)
        return payload
    except ReleaseAuthorityRejected:
        raise
    except OSError:
        _reject(reason_code)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_authority(path: Path) -> dict[str, Any]:
    return _validate_authority_receipt(
        _json_bytes(
            _read_regular(path, maximum=MAX_API_BYTES, reason_code="release_authority_file"),
            maximum=MAX_API_BYTES,
            reason_code="release_authority_json",
        )
    )


def _load_policy_receipt(path: Path) -> dict[str, Any]:
    return _validate_policy_receipt(
        _json_bytes(
            _read_regular(path, maximum=MAX_API_BYTES, reason_code="release_repository_policy_receipt_file"),
            maximum=MAX_API_BYTES,
            reason_code="release_repository_policy_receipt_json",
        )
    )


def load_repository_policy(directory: Path) -> dict[str, Any]:
    """Load and validate one bounded raw GitHub repository-policy snapshot."""

    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _reject("release_repository_policy_files")
    names = {entry.name for entry in entries}
    if len(names) != len(entries) or not {"immutable.json", "rulesets.json"} <= names:
        _reject("release_repository_policy_files")
    immutable = _json_bytes(
        _read_regular(
            directory / "immutable.json",
            maximum=MAX_API_BYTES,
            reason_code="release_immutability_authority",
        ),
        maximum=MAX_API_BYTES,
        reason_code="release_immutability_authority",
    )
    summaries = _json_bytes(
        _read_regular(
            directory / "rulesets.json",
            maximum=MAX_API_BYTES,
            reason_code="release_repository_policy_files",
        ),
        maximum=MAX_API_BYTES,
        reason_code="release_repository_policy",
    )
    if type(summaries) is not list or not 1 <= len(summaries) < 100:
        _reject("release_repository_policy")
    details: dict[int, Any] = {}
    expected_names = {"immutable.json", "rulesets.json"}
    for raw in summaries:
        summary = _exact_mapping(raw, {"id"}, "release_repository_policy")
        ruleset_id = _positive_integer(summary["id"], "release_repository_policy")
        if ruleset_id in details:
            _reject("release_repository_policy")
        name = f"ruleset-{ruleset_id}.json"
        expected_names.add(name)
        details[ruleset_id] = _json_bytes(
            _read_regular(
                directory / name,
                maximum=MAX_API_BYTES,
                reason_code="release_repository_policy_files",
            ),
            maximum=MAX_API_BYTES,
            reason_code="release_repository_policy",
        )
    if names != expected_names:
        _reject("release_repository_policy_files")
    return validate_repository_policy(immutable=immutable, summaries=summaries, details=details)


def verify_report(authority: Mapping[str, Any], report_path: Path) -> str:
    """Pre-bind exact report bytes before Henry reconstructs the full report."""

    validated = _validate_authority_receipt(authority)
    try:
        siblings = tuple(report_path.parent.iterdir())
    except OSError:
        _reject("release_report_directory")
    if siblings != (report_path,):
        _reject("release_report_directory")
    payload = _read_regular(report_path, maximum=MAX_REPORT_BYTES, reason_code="release_report_file")
    report = _json_bytes(payload, maximum=MAX_REPORT_BYTES, reason_code="release_report_json")
    if type(report) is not dict:
        _reject("release_report_shape")
    runner = _exact_mapping(
        report.get("runner"),
        {
            "event_name",
            "native_platform",
            "ref_protected",
            "repository",
            "repository_id",
            "run_attempt",
            "run_id",
            "runner_arch",
            "runner_environment",
            "runner_os",
            "source_ref",
            "source_revision",
            "workflow_revision",
            "workflow_ref_digest",
        },
        "release_report_runner",
    )
    source = validated["source"]
    boron = validated["boron"]
    workflow_ref_digest = "sha256:" + hashlib.sha256(CI_WORKFLOW_REF.encode("utf-8")).hexdigest()
    expected_runner = {
        "event_name": "push",
        "native_platform": "linux/amd64",
        "ref_protected": True,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "run_attempt": boron["run_attempt"],
        "run_id": boron["run_id"],
        "runner_arch": "X64",
        "runner_environment": "github-hosted",
        "runner_os": "Linux",
        "source_ref": DEFAULT_REF,
        "source_revision": source["revision"],
        "workflow_revision": source["revision"],
        "workflow_ref_digest": workflow_ref_digest,
    }
    if (
        report.get("schema_version") != 2
        or report.get("status") != "passed"
        or report.get("public_claim_eligible") is not False
        or runner != expected_runner
    ):
        _reject("release_report_binding")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _verify_henry_report(authority: Mapping[str, Any], report_path: Path, expected_digest: str) -> None:
    """Run Henry's full reconstruction under the exact upstream run context."""

    validated = _validate_authority_receipt(authority)
    source = validated["source"]
    boron = validated["boron"]
    environment = os.environ.copy()
    environment.update(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_REPOSITORY_ID": str(REPOSITORY_ID),
            "GITHUB_REF": DEFAULT_REF,
            "GITHUB_REF_PROTECTED": "true",
            "GITHUB_RUN_ATTEMPT": str(boron["run_attempt"]),
            "GITHUB_RUN_ID": str(boron["run_id"]),
            "GITHUB_SHA": source["revision"],
            "GITHUB_WORKFLOW_SHA": source["revision"],
            "GITHUB_WORKFLOW_REF": CI_WORKFLOW_REF,
            "RUNNER_ARCH": "X64",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux",
        }
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(ROOT / "scripts" / "henry_boron_hosted_qualification.py"),
        "--verify-report",
        str(report_path),
        "--subject-digest-only",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        _reject("release_report_semantics")
    if completed.returncode != 0 or completed.stderr != "" or completed.stdout != expected_digest + "\n":
        _reject("release_report_semantics")


def validated_attestation_bundle(
    authority: Mapping[str, Any],
    *,
    report_digest: str,
    verification: Any,
) -> bytes:
    """Retain the one cryptographically verified same-run repository bundle."""

    validated = _validate_authority_receipt(authority)
    digest = _digest(report_digest, "release_attestation_subject_digest")
    if type(verification) is not list or len(verification) != 1:
        _reject("release_attestation_count")
    row = _exact_mapping(
        verification[0],
        {"attestation", "verificationResult"},
        "release_attestation_shape",
    )
    attestation = _exact_mapping(row["attestation"], {"bundle"}, "release_attestation_shape")
    result = _exact_mapping(
        row["verificationResult"],
        {"mediaType", "statement", "signature", "verifiedTimestamps"},
        "release_attestation_result",
    )
    if result["mediaType"] != SIGSTORE_VERIFICATION_RESULT_MEDIA_TYPE:
        _reject("release_attestation_result")
    statement = _exact_mapping(
        result["statement"],
        {"_type", "subject", "predicateType", "predicate"},
        "release_attestation_statement",
    )
    subjects = statement["subject"]
    if type(subjects) is not list or len(subjects) != 1:
        _reject("release_attestation_subject")
    subject = _exact_mapping(subjects[0], {"name", "digest"}, "release_attestation_subject")
    subject_digests = subject["digest"]
    if (
        statement["_type"] != IN_TOTO_STATEMENT_V1
        or statement["predicateType"] != SLSA_PROVENANCE_V1
        or type(subject_digests) is not dict
        or subject["name"] != BORON_REPORT_NAME
        or subject_digests != {"sha256": digest.removeprefix("sha256:")}
    ):
        _reject("release_attestation_subject")

    signature = _exact_mapping(result["signature"], {"certificate"}, "release_attestation_certificate")
    certificate = _exact_mapping(
        signature["certificate"],
        {
            "subjectAlternativeName",
            "buildSignerURI",
            "buildSignerDigest",
            "runnerEnvironment",
            "sourceRepositoryURI",
            "sourceRepositoryDigest",
            "sourceRepositoryRef",
            "sourceRepositoryIdentifier",
            "buildTrigger",
            "runInvocationURI",
        },
        "release_attestation_certificate",
    )
    source_revision = validated["source"]["revision"]
    boron = validated["boron"]
    run_uri = f"https://github.com/{REPOSITORY}/actions/runs/{boron['run_id']}/attempts/{boron['run_attempt']}"
    if (
        certificate["subjectAlternativeName"] != CI_WORKFLOW_IDENTITY
        or certificate["buildSignerURI"] != CI_WORKFLOW_IDENTITY
        or certificate["buildSignerDigest"] != source_revision
        or certificate["runnerEnvironment"] != "github-hosted"
        or certificate["sourceRepositoryURI"] != f"https://github.com/{REPOSITORY}"
        or certificate["sourceRepositoryDigest"] != source_revision
        or certificate["sourceRepositoryRef"] != DEFAULT_REF
        or certificate["sourceRepositoryIdentifier"] != str(REPOSITORY_ID)
        or certificate["buildTrigger"] != "push"
        or certificate["runInvocationURI"] != run_uri
    ):
        _reject("release_attestation_certificate")
    timestamps = result["verifiedTimestamps"]
    if type(timestamps) is not list or not timestamps:
        _reject("release_attestation_timestamp")
    run_started_at = _timestamp(boron["run_started_at"], github=True, reason_code="release_attestation_timestamp")
    run_completed_at = _timestamp(boron["run_completed_at"], github=True, reason_code="release_attestation_timestamp")
    for raw_timestamp in timestamps:
        if type(raw_timestamp) is not dict or set(raw_timestamp) != {"type", "uri", "timestamp"}:
            _reject("release_attestation_timestamp")
        if raw_timestamp["type"] not in {"Tlog", "TimestampAuthority"}:
            _reject("release_attestation_timestamp")
        _https_uri(raw_timestamp["uri"], "release_attestation_timestamp")
        observed_at = _timestamp(
            raw_timestamp["timestamp"],
            github=False,
            reason_code="release_attestation_timestamp",
        )
        if not run_started_at - ATTESTATION_CLOCK_SKEW <= observed_at <= run_completed_at + ATTESTATION_CLOCK_SKEW:
            _reject("release_attestation_timestamp")

    bundle = _exact_mapping(
        attestation["bundle"],
        {"mediaType", "verificationMaterial", "dsseEnvelope"},
        "release_attestation_bundle",
    )
    if (
        bundle["mediaType"] not in SIGSTORE_BUNDLE_MEDIA_TYPES
        or type(bundle["verificationMaterial"]) is not dict
        or not bundle["verificationMaterial"]
    ):
        _reject("release_attestation_bundle")
    envelope = bundle["dsseEnvelope"]
    if type(envelope) is not dict or set(envelope) != {"payload", "payloadType", "signatures"}:
        _reject("release_attestation_bundle")
    payload_text = envelope["payload"]
    signatures = envelope["signatures"]
    if envelope["payloadType"] != DSSE_IN_TOTO_PAYLOAD_TYPE or type(payload_text) is not str:
        _reject("release_attestation_bundle")
    if type(signatures) is not list or not signatures:
        _reject("release_attestation_bundle")
    for raw_signature in signatures:
        if type(raw_signature) is not dict or "sig" not in raw_signature or not set(raw_signature) <= {"sig", "keyid"}:
            _reject("release_attestation_bundle")
        signature_text = raw_signature["sig"]
        if type(signature_text) is not str or not signature_text:
            _reject("release_attestation_bundle")
        try:
            decoded_signature = base64.b64decode(signature_text, validate=True)
        except (binascii.Error, UnicodeError, ValueError):
            _reject("release_attestation_bundle")
        if not decoded_signature:
            _reject("release_attestation_bundle")
    try:
        decoded_payload = base64.b64decode(payload_text, validate=True)
    except (binascii.Error, UnicodeError, ValueError):
        _reject("release_attestation_bundle")
    bundle_statement = _json_bytes(
        decoded_payload,
        maximum=MAX_ATTESTATION_BYTES,
        reason_code="release_attestation_bundle_statement",
    )
    if _canonical(bundle_statement) != _canonical(statement):
        _reject("release_attestation_bundle_statement")
    encoded = _canonical(bundle) + b"\n"
    if not 1 <= len(encoded) <= MAX_ATTESTATION_BYTES:
        _reject("release_attestation_bundle")
    return encoded


def _release_statement_subjects(statement: Mapping[str, Any], distributions: Mapping[str, Any]) -> None:
    subjects = statement.get("subject")
    if type(subjects) is not list or len(subjects) != len(distributions):
        _reject("release_bundle_subject")
    observed: dict[str, str] = {}
    for raw_subject in subjects:
        subject = _exact_mapping(raw_subject, {"name", "digest"}, "release_bundle_subject")
        name = subject["name"]
        digest = subject["digest"]
        if (
            type(name) is not str
            or name not in distributions
            or name in observed
            or type(digest) is not dict
            or set(digest) != {"sha256"}
            or digest["sha256"] != distributions[name]["digest"]
        ):
            _reject("release_bundle_subject")
        observed[name] = digest["sha256"]
    if set(observed) != set(distributions):
        _reject("release_bundle_subject")


def _local_sigstore_bundle(path: Path) -> Any:
    payload = _read_regular(path, maximum=MAX_ATTESTATION_BYTES, reason_code="release_bundle_file")
    lines = payload.splitlines()
    if len(lines) != 1 or not lines[0]:
        _reject("release_bundle_file")
    return _json_bytes(lines[0], maximum=MAX_ATTESTATION_BYTES, reason_code="release_bundle_json")


def _validate_release_verification(
    *,
    path: Path,
    bundle_path: Path,
    distributions: Mapping[str, Any],
    predicate_type: str,
    predicate: Any | None,
    source_revision: str,
) -> tuple[str, bytes]:
    verification = _json_bytes(
        _read_regular(path, maximum=MAX_ATTESTATION_BYTES, reason_code="release_bundle_verification_file"),
        maximum=MAX_ATTESTATION_BYTES,
        reason_code="release_bundle_verification_json",
    )
    if type(verification) is not list or len(verification) != 1:
        _reject("release_bundle_verification_count")
    row = _exact_mapping(
        verification[0],
        {"attestation", "verificationResult"},
        "release_bundle_verification_shape",
    )
    attestation = _exact_mapping(row["attestation"], {"bundle"}, "release_bundle_verification_shape")
    result = _exact_mapping(
        row["verificationResult"],
        {"mediaType", "statement", "signature", "verifiedTimestamps"},
        "release_bundle_verification_shape",
    )
    if result["mediaType"] != SIGSTORE_VERIFICATION_RESULT_MEDIA_TYPE:
        _reject("release_bundle_verification_shape")
    statement = _exact_mapping(
        result["statement"],
        {"_type", "subject", "predicateType", "predicate"},
        "release_bundle_statement",
    )
    if statement["_type"] != IN_TOTO_STATEMENT_V1 or statement["predicateType"] != predicate_type:
        _reject("release_bundle_statement")
    _release_statement_subjects(statement, distributions)
    if predicate is not None and _canonical(statement["predicate"]) != _canonical(predicate):
        _reject("release_bundle_predicate")

    signature = _exact_mapping(result["signature"], {"certificate"}, "release_bundle_certificate")
    certificate = _exact_mapping(
        signature["certificate"],
        {
            "subjectAlternativeName",
            "buildSignerURI",
            "buildSignerDigest",
            "runnerEnvironment",
            "sourceRepositoryURI",
            "sourceRepositoryDigest",
            "sourceRepositoryRef",
            "sourceRepositoryIdentifier",
            "buildTrigger",
            "runInvocationURI",
        },
        "release_bundle_certificate",
    )
    run_uri = certificate["runInvocationURI"]
    if (
        certificate["subjectAlternativeName"] != RELEASE_WORKFLOW_IDENTITY
        or certificate["buildSignerURI"] != RELEASE_WORKFLOW_IDENTITY
        or certificate["buildSignerDigest"] != source_revision
        or certificate["runnerEnvironment"] != "github-hosted"
        or certificate["sourceRepositoryURI"] != f"https://github.com/{REPOSITORY}"
        or certificate["sourceRepositoryDigest"] != source_revision
        or certificate["sourceRepositoryRef"] != DEFAULT_REF
        or certificate["sourceRepositoryIdentifier"] != str(REPOSITORY_ID)
        or certificate["buildTrigger"] != "workflow_dispatch"
        or type(run_uri) is not str
        or re.fullmatch(
            rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*",
            run_uri,
        )
        is None
    ):
        _reject("release_bundle_certificate")
    timestamps = result["verifiedTimestamps"]
    if type(timestamps) is not list or not timestamps:
        _reject("release_bundle_timestamp")
    for raw_timestamp in timestamps:
        timestamp = _exact_mapping(raw_timestamp, {"type", "uri", "timestamp"}, "release_bundle_timestamp")
        if timestamp["type"] not in {"Tlog", "TimestampAuthority"}:
            _reject("release_bundle_timestamp")
        _https_uri(timestamp["uri"], "release_bundle_timestamp")
        _timestamp(timestamp["timestamp"], github=False, reason_code="release_bundle_timestamp")

    local_bundle = _local_sigstore_bundle(bundle_path)
    if _canonical(attestation["bundle"]) != _canonical(local_bundle):
        _reject("release_bundle_mismatch")
    bundle = _exact_mapping(
        local_bundle,
        {"mediaType", "verificationMaterial", "dsseEnvelope"},
        "release_bundle_shape",
    )
    envelope = _exact_mapping(
        bundle["dsseEnvelope"],
        {"payload", "payloadType", "signatures"},
        "release_bundle_shape",
    )
    if bundle["mediaType"] not in SIGSTORE_BUNDLE_MEDIA_TYPES or envelope["payloadType"] != DSSE_IN_TOTO_PAYLOAD_TYPE:
        _reject("release_bundle_shape")
    payload_text = envelope["payload"]
    if type(payload_text) is not str:
        _reject("release_bundle_shape")
    try:
        bundle_statement = _json_bytes(
            base64.b64decode(payload_text, validate=True),
            maximum=MAX_ATTESTATION_BYTES,
            reason_code="release_bundle_statement",
        )
    except (binascii.Error, UnicodeError, ValueError):
        _reject("release_bundle_statement")
    if _canonical(bundle_statement) != _canonical(statement):
        _reject("release_bundle_statement")
    return run_uri, _canonical(statement)


def validate_release_attestations(
    *,
    authority_path: Path,
    dist_directory: Path,
    asset_directory: Path,
    verification_directory: Path,
) -> None:
    """Require all durable release bundles to verify the exact bound distributions."""

    authority = _load_authority(authority_path)
    source_revision = authority["source"]["revision"]
    tag = authority["release"]["tag"]
    distributions = _local_distributions(dist_directory, tag)
    predicates = {
        "provenance": (SLSA_PROVENANCE_V1, None, "algo-cli-release-provenance.sigstore.jsonl"),
        "sbom": (
            CYCLONEDX_PREDICATE,
            _json_bytes(
                _read_regular(
                    asset_directory / "algo-cli-runtime.lock.cdx.json",
                    maximum=MAX_REPORT_BYTES,
                    reason_code="release_bundle_predicate_file",
                ),
                maximum=MAX_REPORT_BYTES,
                reason_code="release_bundle_predicate_json",
            ),
            "algo-cli-release-sbom.sigstore.jsonl",
        ),
        "boron": (
            BORON_RELEASE_PREDICATE,
            _json_bytes(
                _read_regular(
                    asset_directory / BORON_REPORT_NAME,
                    maximum=MAX_REPORT_BYTES,
                    reason_code="release_bundle_predicate_file",
                ),
                maximum=MAX_REPORT_BYTES,
                reason_code="release_bundle_predicate_json",
            ),
            "grace-boron-release-qualification.sigstore.jsonl",
        ),
        "source-binding": (
            SOURCE_BINDING_PREDICATE,
            _json_bytes(
                _read_regular(
                    asset_directory / "oliver-release-source-binding.json",
                    maximum=MAX_REPORT_BYTES,
                    reason_code="release_bundle_predicate_file",
                ),
                maximum=MAX_REPORT_BYTES,
                reason_code="release_bundle_predicate_json",
            ),
            "oliver-release-source-binding.sigstore.jsonl",
        ),
    }
    expected_files = {f"{label}-{kind}.json" for label in predicates for kind in ("wheel", "sdist")}
    try:
        verification_entries = tuple(verification_directory.iterdir())
    except OSError:
        _reject("release_bundle_verification_directory")
    if {entry.name for entry in verification_entries} != expected_files:
        _reject("release_bundle_verification_directory")

    observed_run: str | None = None
    for label, (predicate_type, predicate, bundle_name) in predicates.items():
        observed_statement: bytes | None = None
        for kind in ("wheel", "sdist"):
            run_uri, statement = _validate_release_verification(
                path=verification_directory / f"{label}-{kind}.json",
                bundle_path=asset_directory / bundle_name,
                distributions=distributions,
                predicate_type=predicate_type,
                predicate=predicate,
                source_revision=source_revision,
            )
            if observed_run is None:
                observed_run = run_uri
            elif observed_run != run_uri:
                _reject("release_bundle_run")
            if observed_statement is None:
                observed_statement = statement
            elif observed_statement != statement:
                _reject("release_bundle_statement")


def _local_distributions(directory: Path, tag: str) -> dict[str, dict[str, Any]]:
    expected = {name for name in expected_release_assets(tag) if name.endswith((".whl", ".tar.gz"))}
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        _reject("release_distribution_directory")
    if {entry.name for entry in entries} != expected:
        _reject("release_distribution_set")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        payload = _read_regular(entry, maximum=MAX_ARTIFACT_BYTES, reason_code="release_distribution_file")
        result[entry.name] = {
            "digest": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return result


def verify_pypi_state(
    *,
    tag: str,
    directory: Path,
    require_present: bool,
    fetch: Callable[[str], bytes] | None = None,
) -> str:
    """Return absent/partial-exact/exact, rejecting every conflicting PyPI file."""

    distributions = _local_distributions(directory, tag)
    url = f"https://pypi.org/pypi/algo-cli-runtime/{_version(tag)}/json"

    def default_fetch(target: str) -> bytes:
        request = Request(
            target,
            headers={"Accept": "application/json", "User-Agent": "algo-cli-release-authority/1"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS authority
                if response.status != 200 or response.headers.get_content_type() != "application/json":
                    _reject("release_pypi_response")
                payload = response.read(MAX_API_BYTES + 1)
        except HTTPError as error:
            if error.code == 404:
                return b""
            _reject("release_pypi_response")
        except OSError:
            _reject("release_pypi_response")
        return payload

    payload = (default_fetch if fetch is None else fetch)(url)
    if payload == b"":
        if require_present:
            _reject("release_pypi_missing")
        return "absent"
    document = _json_bytes(payload, maximum=MAX_API_BYTES, reason_code="release_pypi_json")
    root = _exact_mapping(document, {"info", "urls"}, "release_pypi_shape")
    info = _exact_mapping(root["info"], {"name", "version"}, "release_pypi_shape")
    if info["name"] != "algo-cli-runtime" or info["version"] != _version(tag):
        _reject("release_pypi_identity")
    urls = root["urls"]
    if type(urls) is not list or not 1 <= len(urls) <= len(distributions):
        _reject("release_pypi_distribution_set")
    observed: dict[str, dict[str, Any]] = {}
    for raw in urls:
        file = _exact_mapping(
            raw,
            {"filename", "digests", "size", "packagetype", "yanked"},
            "release_pypi_file",
        )
        filename = file["filename"]
        digests = file["digests"]
        if (
            type(filename) is not str
            or filename in observed
            or filename not in distributions
            or type(digests) is not dict
            or set(digests) < {"sha256"}
            or file["yanked"] is not False
            or file["packagetype"] != ("bdist_wheel" if filename.endswith(".whl") else "sdist")
        ):
            _reject("release_pypi_file")
        observed[filename] = {
            "digest": digests["sha256"],
            "size": file["size"],
        }
    for filename, observed_file in observed.items():
        if observed_file != distributions[filename]:
            _reject("release_pypi_digest")
    if set(observed) == set(distributions):
        return "exact"
    if require_present:
        _reject("release_pypi_missing")
    return "partial-exact"


def _gh_api(endpoint: str) -> Any:
    if type(endpoint) is not str or not endpoint.startswith(f"repos/{REPOSITORY}"):
        _reject("release_api_endpoint")
    command = [
        os.environ.get("OLIVER_GH_BIN", "gh"),
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        endpoint,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _reject("release_api_request")
    if completed.returncode != 0 or not 1 <= len(completed.stdout) <= MAX_API_BYTES:
        _reject("release_api_request")
    return _json_bytes(completed.stdout, maximum=MAX_API_BYTES, reason_code="release_api_json")


def _git_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=ROOT,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        _reject("release_checkout_revision")
    if completed.returncode != 0:
        _reject("release_checkout_revision")
    return _revision(completed.stdout.strip(), "release_checkout_revision")


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid)


def _write_all(descriptor: int, payload: bytes, reason_code: str) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except OSError:
            _reject(reason_code)
        if written <= 0:
            _reject(reason_code)
        offset += written


def _open_parent(path: Path, reason_code: str) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        before = path.parent.lstat()
        if not stat.S_ISDIR(before.st_mode):
            _reject(reason_code)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        current = path.parent.lstat()
        if _directory_identity(opened) != _directory_identity(before) or _directory_identity(
            current
        ) != _directory_identity(opened):
            _reject(reason_code)
        return descriptor, opened
    except ReleaseAuthorityRejected:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _reject(reason_code)


def _atomic_write(path: Path, payload: bytes) -> None:
    if type(payload) is not bytes or not payload:
        _reject("release_output_write")
    parent_descriptor, parent = _open_parent(path, "release_output_write")
    descriptor = -1
    opened: os.stat_result | None = None
    completed = False
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size != 0:
            _reject("release_output_write")
        _write_all(descriptor, payload, "release_output_write")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        current_parent = path.parent.lstat()
        if (
            _stable_file_identity(after) != _stable_file_identity(current)
            or after.st_size != len(payload)
            or stat.S_IMODE(after.st_mode) != 0o600
            or _directory_identity(current_parent) != _directory_identity(parent)
        ):
            _reject("release_output_write")
        os.fsync(parent_descriptor)
        completed = True
    except FileExistsError:
        _reject("release_output_exists")
    except ReleaseAuthorityRejected:
        raise
    except OSError:
        _reject("release_output_write")
    finally:
        if opened is not None and not completed:
            try:
                current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                if _same_file_object(current, opened):
                    os.unlink(path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _append_outputs(path: Path, rows: Mapping[str, str]) -> None:
    for key, value in rows.items():
        if (
            re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key) is None
            or type(value) is not str
            or not value
            or any(character in value for character in "\r\n\x00")
        ):
            _reject("release_github_output")
    payload = "".join(f"{key}={value}\n" for key, value in rows.items()).encode("utf-8")
    parent_descriptor, parent = _open_parent(path, "release_github_output")
    descriptor = -1
    try:
        before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 <= before.st_size <= MAX_GITHUB_OUTPUT_BYTES - len(payload)
        ):
            _reject("release_github_output")
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if _stable_file_identity(opened) != _stable_file_identity(before):
            _reject("release_github_output")
        _write_all(descriptor, payload, "release_github_output")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        current_parent = path.parent.lstat()
        if (
            (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode), after.st_nlink)
            != (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode), opened.st_nlink)
            or _stable_file_identity(after) != _stable_file_identity(current)
            or after.st_size != opened.st_size + len(payload)
            or _directory_identity(current_parent) != _directory_identity(parent)
        ):
            _reject("release_github_output")
    except ReleaseAuthorityRejected:
        raise
    except OSError:
        _reject("release_github_output")
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _receipt_outputs(receipt: Mapping[str, Any], *, release_state: str) -> dict[str, str]:
    return {
        "release-id": str(receipt["release"]["id"]),
        "release-state": release_state,
        "source-sha": receipt["source"]["revision"],
        "boron-run-id": str(receipt["boron"]["run_id"]),
        "boron-run-attempt": str(receipt["boron"]["run_attempt"]),
        "boron-artifact-id": str(receipt["boron"]["artifact_id"]),
        "boron-artifact-digest": receipt["boron"]["artifact_digest"],
    }


def _require_release_platform(platform_name: str | None = None) -> None:
    """Keep the release authority on its descriptor-capable POSIX boundary."""

    selected = os.name if platform_name is None else platform_name
    if selected != "posix":
        _reject("release_platform_unsupported")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    dispatch = modes.add_parser("dispatch")
    dispatch.add_argument("--github-output", type=Path, required=True)

    policy = modes.add_parser("policy")
    policy.add_argument("--directory", type=Path, required=True)
    policy.add_argument("--output", type=Path, required=True)

    authority = modes.add_parser("authority")
    authority.add_argument("--tag", required=True)
    authority.add_argument("--policy", type=Path, required=True)
    authority.add_argument("--output", type=Path, required=True)
    authority.add_argument("--github-output", type=Path, required=True)

    report = modes.add_parser("report")
    report.add_argument("--authority", type=Path, required=True)
    report.add_argument("--report", type=Path, required=True)
    report.add_argument("--github-output", type=Path, required=True)

    attestation = modes.add_parser("attestation")
    attestation.add_argument("--authority", type=Path, required=True)
    attestation.add_argument("--report-digest", required=True)
    attestation.add_argument("--verification", type=Path, required=True)
    attestation.add_argument("--output", type=Path, required=True)

    pypi = modes.add_parser("pypi")
    pypi.add_argument("--tag", required=True)
    pypi.add_argument("--dist", type=Path, required=True)
    pypi.add_argument("--require-present", action="store_true")
    pypi.add_argument("--github-output", type=Path)

    asset_plan = modes.add_parser("asset-plan")
    asset_plan.add_argument("--tag", required=True)
    asset_plan.add_argument("--release-id", type=int, required=True)
    asset_plan.add_argument("--release-state", required=True)
    asset_plan.add_argument("--release-json", type=Path, required=True)
    asset_plan.add_argument("--output", type=Path, required=True)

    reconcile = modes.add_parser("reconcile")
    reconcile.add_argument("--tag", required=True)
    reconcile.add_argument("--release-id", type=int, required=True)
    reconcile.add_argument("--release-state", required=True)
    reconcile.add_argument("--source-sha", required=True)
    reconcile.add_argument("--release-json", type=Path, required=True)
    reconcile.add_argument("--directory", type=Path, required=True)
    reconcile.add_argument("--authority", type=Path, required=True)
    reconcile.add_argument("--policy", type=Path, required=True)
    reconcile.add_argument("--report", type=Path, required=True)
    reconcile.add_argument("--boron-bundle", type=Path, required=True)

    release_attestations = modes.add_parser("release-attestations")
    release_attestations.add_argument("--authority", type=Path, required=True)
    release_attestations.add_argument("--dist", type=Path, required=True)
    release_attestations.add_argument("--assets", type=Path, required=True)
    release_attestations.add_argument("--verifications", type=Path, required=True)

    closure = modes.add_parser("asset-closure")
    closure.add_argument("--tag", required=True)
    closure.add_argument("--directory", type=Path, required=True)

    arguments = parser.parse_args(argv)
    try:
        _require_release_platform()
        if arguments.mode == "dispatch":
            context = DispatchContext.from_environment(dict(os.environ))
            checkout = _git_head()
            if checkout != context.source_revision:
                _reject("release_checkout_revision")
            _append_outputs(arguments.github_output, {"source-sha": context.source_revision})
            print(json.dumps({"status": "passed"}, sort_keys=True))
        elif arguments.mode == "policy":
            policy_receipt = load_repository_policy(arguments.directory)
            _atomic_write(arguments.output, _canonical(policy_receipt) + b"\n")
            print(json.dumps({"status": "passed"}, sort_keys=True))
        elif arguments.mode == "authority":
            receipt, release_state = validate_authority(
                tag=arguments.tag,
                environment=dict(os.environ),
                checkout_revision=_git_head(),
                policy_receipt=_load_policy_receipt(arguments.policy),
                api_get=_gh_api,
            )
            _atomic_write(arguments.output, _canonical(receipt) + b"\n")
            _append_outputs(arguments.github_output, _receipt_outputs(receipt, release_state=release_state))
            print(json.dumps({"status": "passed"}, sort_keys=True))
        elif arguments.mode == "report":
            authority_receipt = _load_authority(arguments.authority)
            report_digest = verify_report(authority_receipt, arguments.report)
            _verify_henry_report(authority_receipt, arguments.report, report_digest)
            _append_outputs(arguments.github_output, {"report-digest": report_digest})
            print(report_digest)
        elif arguments.mode == "attestation":
            verification = _json_bytes(
                _read_regular(
                    arguments.verification,
                    maximum=MAX_ATTESTATION_BYTES,
                    reason_code="release_attestation_verification_file",
                ),
                maximum=MAX_ATTESTATION_BYTES,
                reason_code="release_attestation_verification_json",
            )
            bundle = validated_attestation_bundle(
                _load_authority(arguments.authority),
                report_digest=arguments.report_digest,
                verification=verification,
            )
            _atomic_write(arguments.output, bundle)
            print("passed")
        elif arguments.mode == "pypi":
            state = verify_pypi_state(
                tag=arguments.tag,
                directory=arguments.dist,
                require_present=arguments.require_present,
            )
            if arguments.github_output is not None:
                _append_outputs(arguments.github_output, {"pypi-state": state})
            print(state)
        elif arguments.mode == "asset-plan":
            release = _json_bytes(
                _read_regular(
                    arguments.release_json,
                    maximum=MAX_API_BYTES,
                    reason_code="release_reconcile_release_file",
                ),
                maximum=MAX_API_BYTES,
                reason_code="release_reconcile_release_json",
            )
            plan = durable_asset_plan(
                tag=arguments.tag,
                release_id=arguments.release_id,
                release_state=arguments.release_state,
                release=release,
            )
            _atomic_write(arguments.output, _canonical(plan) + b"\n")
            print(json.dumps({"status": "passed"}, sort_keys=True))
        elif arguments.mode == "reconcile":
            release = _json_bytes(
                _read_regular(
                    arguments.release_json,
                    maximum=MAX_API_BYTES,
                    reason_code="release_reconcile_release_file",
                ),
                maximum=MAX_API_BYTES,
                reason_code="release_reconcile_release_json",
            )
            result = validate_durable_assets(
                tag=arguments.tag,
                release_id=arguments.release_id,
                release_state=arguments.release_state,
                source_revision=arguments.source_sha,
                release=release,
                directory=arguments.directory,
                authority_path=arguments.authority,
                policy_path=arguments.policy,
                report_path=arguments.report,
                boron_bundle_path=arguments.boron_bundle,
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        elif arguments.mode == "release-attestations":
            validate_release_attestations(
                authority_path=arguments.authority,
                dist_directory=arguments.dist,
                asset_directory=arguments.assets,
                verification_directory=arguments.verifications,
            )
            print(json.dumps({"status": "passed"}, sort_keys=True))
        else:
            print(
                json.dumps(
                    validate_release_asset_closure(tag=arguments.tag, directory=arguments.directory),
                    sort_keys=True,
                )
            )
        return 0
    except ReleaseAuthorityRejected as error:
        print(
            json.dumps(
                {"reason_code": error.reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"reason_code": "release_internal_error", "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
