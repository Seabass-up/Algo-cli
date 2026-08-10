from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "oliver_release_authority.py"
SPEC = importlib.util.spec_from_file_location("oliver_release_authority_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)

REVISION = "a" * 40
TAG = "v0.19.0"
REPORT_DIGEST = "sha256:" + "b" * 64
RULESET_ID = 701


def _yaml_duplicate_mapping_keys(document: str) -> tuple[tuple[int, str], ...]:
    """Parse this workflow's indentation scopes without discarding duplicate keys."""

    frames: dict[int, set[str]] = {}
    duplicates: list[tuple[int, str]] = []
    block_scalar_indent: int | None = None
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$")
    for line_number, line in enumerate(document.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_scalar_indent is not None:
            if indent > block_scalar_indent:
                continue
            block_scalar_indent = None
        content = line.strip()
        if content.startswith("- "):
            logical_indent = indent + 2
            for level in tuple(frames):
                if level >= logical_indent:
                    del frames[level]
            content = content[2:].lstrip()
        else:
            logical_indent = indent
            for level in tuple(frames):
                if level > logical_indent:
                    del frames[level]
        match = key_pattern.fullmatch(content)
        if match is None:
            continue
        key, raw_value = match.groups()
        seen = frames.setdefault(logical_indent, set())
        if key in seen:
            duplicates.append((line_number, key))
        seen.add(key)
        if raw_value is not None and re.fullmatch(r"[>|](?:[+-]?[0-9]?|[0-9]?[+-]?)", raw_value):
            block_scalar_indent = indent
    return tuple(duplicates)


def _workflow_job_bodies(document: str) -> dict[str, str]:
    matches = tuple(re.finditer(r"^  ([a-z][a-z0-9-]*):\n", document, flags=re.MULTILINE))
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        bodies[match.group(1)] = document[match.end() : end]
    return bodies


def _workflow_job_outputs(document: str) -> dict[str, frozenset[str]]:
    outputs: dict[str, frozenset[str]] = {}
    for job, body in _workflow_job_bodies(document).items():
        section = re.search(r"^    outputs:\n((?:^      [a-z][a-z0-9-]*:.*\n)+)", body, flags=re.MULTILINE)
        outputs[job] = frozenset(
            re.findall(r"^      ([a-z][a-z0-9-]*):", section.group(1), flags=re.MULTILINE)
            if section is not None
            else ()
        )
    return outputs


def _post_publish_validator(workflow: str) -> str:
    marker = 'STAGE="${stage}" STATE="${state}" python -I -B -S - "${RELEASE_TAG}" "${RELEASE_ID}" "${SOURCE_SHA}" after <<\'PY\''
    marker_at = workflow.rindex(marker)
    script_at = workflow.index("\n", marker_at) + 1
    script_end = workflow.index("\n          PY", script_at)
    return textwrap.dedent(workflow[script_at:script_end]) + "\n"


def _environment_authority_validator(workflow: str) -> str:
    marker = 'STATE="${state}" ACTOR_ID="${GITHUB_ACTOR_ID}" python -I -B -S - <<\'PY\''
    marker_at = workflow.index(marker)
    script_at = workflow.index("\n", marker_at) + 1
    script_end = workflow.index("\n          PY", script_at)
    return textwrap.dedent(workflow[script_at:script_end]) + "\n"


def _pre_pypi_asset_validator(workflow: str) -> str:
    marker = (
        'STATE="${state}" ASSETS="${RUNNER_TEMP}/verified-release-assets" PACKAGE="${RUNNER_TEMP}/package/bound-dist"'
    )
    marker_at = workflow.index(marker)
    script_at = workflow.index("\n", marker_at) + 1
    script_end = workflow.index("\n          PY", script_at)
    return textwrap.dedent(workflow[script_at:script_end]) + "\n"


def _immediate_pypi_validator(workflow: str) -> str:
    marker = 'STATE="${state}" DIST="${RUNNER_TEMP}/package/bound-dist" RELEASE_TAG="${RELEASE_TAG}" HTTP_STATUS="${http_status}"'
    marker_at = workflow.index(marker)
    script_at = workflow.index("\n", marker_at) + 1
    script_end = workflow.index("\n          PY", script_at)
    return textwrap.dedent(workflow[script_at:script_end]) + "\n"


def _final_pypi_validator(workflow: str) -> str:
    marker = (
        'STAGE="${stage}" PYPI="${state}/pypi-${label}.json" RELEASE_TAG="${RELEASE_TAG}" python -I -B -S - <<\'PY\''
    )
    marker_at = workflow.index(marker)
    script_at = workflow.index("\n", marker_at) + 1
    script_end = workflow.index("\n          PY", script_at)
    return textwrap.dedent(workflow[script_at:script_end]) + "\n"


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": SCRIPT.REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(SCRIPT.REPOSITORY_ID),
        "GITHUB_REF": SCRIPT.DEFAULT_REF,
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": REVISION,
        "GITHUB_WORKFLOW_SHA": REVISION,
        "GITHUB_WORKFLOW_REF": (f"{SCRIPT.REPOSITORY}/.github/workflows/oliver-release.yml@{SCRIPT.DEFAULT_REF}"),
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
    }
    values.update(overrides)
    return values


def _api_documents() -> dict[str, Any]:
    workflow_runs = (
        f"repos/{SCRIPT.REPOSITORY}/actions/workflows/{SCRIPT.CI_WORKFLOW_PATH}/runs"
        f"?branch={SCRIPT.DEFAULT_BRANCH}&event=push&head_sha={REVISION}"
        "&status=success&per_page=100"
    )
    return {
        f"repos/{SCRIPT.REPOSITORY}": {
            "id": SCRIPT.REPOSITORY_ID,
            "full_name": SCRIPT.REPOSITORY,
            "default_branch": SCRIPT.DEFAULT_BRANCH,
            "archived": False,
            "fork": False,
        },
        f"repos/{SCRIPT.REPOSITORY}/branches/{SCRIPT.DEFAULT_BRANCH}": {
            "name": SCRIPT.DEFAULT_BRANCH,
            "protected": True,
            "commit": {"sha": REVISION},
        },
        f"repos/{SCRIPT.REPOSITORY}/git/ref/tags/{TAG}": {
            "ref": f"refs/tags/{TAG}",
            "object": {"sha": REVISION, "type": "commit"},
        },
        f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}": {
            "id": 301,
            "tag_name": TAG,
            "target_commitish": SCRIPT.DEFAULT_BRANCH,
            "draft": True,
            "prerelease": False,
            "immutable": False,
            "published_at": None,
            "assets": [],
        },
        f"repos/{SCRIPT.REPOSITORY}/actions/workflows/{SCRIPT.CI_WORKFLOW_PATH}": {
            "id": 401,
            "name": "CI",
            "path": SCRIPT.CI_WORKFLOW_PATH,
            "state": "active",
        },
        workflow_runs: {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 501,
                    "run_attempt": 2,
                    "workflow_id": 401,
                    "path": SCRIPT.CI_WORKFLOW_PATH,
                    "head_branch": SCRIPT.DEFAULT_BRANCH,
                    "head_sha": REVISION,
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "run_started_at": "2026-08-08T23:59:00Z",
                    "updated_at": "2026-08-09T00:01:00Z",
                    "repository": {
                        "id": SCRIPT.REPOSITORY_ID,
                        "full_name": SCRIPT.REPOSITORY,
                    },
                    "head_repository": {
                        "id": SCRIPT.REPOSITORY_ID,
                        "full_name": SCRIPT.REPOSITORY,
                    },
                }
            ],
        },
        f"repos/{SCRIPT.REPOSITORY}/actions/runs/501/artifacts?name={SCRIPT.BORON_ARTIFACT_NAME}-attempt-2&per_page=100": {
            "total_count": 1,
            "artifacts": [
                {
                    "id": 601,
                    "name": f"{SCRIPT.BORON_ARTIFACT_NAME}-attempt-2",
                    "expired": False,
                    "size_in_bytes": 4096,
                    "digest": "sha256:" + "c" * 64,
                    "workflow_run": {
                        "id": 501,
                        "repository_id": SCRIPT.REPOSITORY_ID,
                        "head_repository_id": SCRIPT.REPOSITORY_ID,
                        "head_branch": SCRIPT.DEFAULT_BRANCH,
                        "head_sha": REVISION,
                    },
                }
            ],
        },
    }


def _repository_policy() -> dict[str, Any]:
    summary = {
        "id": RULESET_ID,
        "name": "Protect public release tags",
        "source": SCRIPT.REPOSITORY,
        "enforcement": "active",
        "target": "tag",
    }
    detail = {
        **summary,
        "source_type": "Repository",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "exclude": [],
                "include": [SCRIPT.RELEASE_TAG_RULESET_PATTERN],
            }
        },
        "rules": [{"type": "deletion"}, {"type": "update"}],
    }
    return SCRIPT.validate_repository_policy(
        immutable={"enabled": True},
        summaries=[summary],
        details={RULESET_ID: detail},
    )


def _release_assets() -> list[dict[str, Any]]:
    return [
        {
            "id": index,
            "name": name,
            "state": "uploaded",
            "size": index,
            "digest": "sha256:" + f"{index:064x}",
        }
        for index, name in enumerate(sorted(SCRIPT.expected_release_assets(TAG)), start=1)
    ]


def _authority_result(documents: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    records = _api_documents() if documents is None else documents
    return SCRIPT.validate_authority(
        tag=TAG,
        environment=_environment(),
        checkout_revision=REVISION,
        policy_receipt=_repository_policy(),
        api_get=lambda endpoint: copy.deepcopy(records[endpoint]),
    )


def _authority(documents: dict[str, Any] | None = None) -> dict[str, Any]:
    return _authority_result(documents)[0]


def _report(authority: dict[str, Any]) -> dict[str, Any]:
    workflow_ref_digest = "sha256:" + hashlib.sha256(SCRIPT.CI_WORKFLOW_REF.encode()).hexdigest()
    return {
        "schema_version": 2,
        "status": "passed",
        "public_claim_eligible": False,
        "runner": {
            "event_name": "push",
            "native_platform": "linux/amd64",
            "ref_protected": True,
            "repository": SCRIPT.REPOSITORY,
            "repository_id": SCRIPT.REPOSITORY_ID,
            "run_attempt": authority["boron"]["run_attempt"],
            "run_id": authority["boron"]["run_id"],
            "runner_arch": "X64",
            "runner_environment": "github-hosted",
            "runner_os": "Linux",
            "source_ref": SCRIPT.DEFAULT_REF,
            "source_revision": REVISION,
            "workflow_revision": REVISION,
            "workflow_ref_digest": workflow_ref_digest,
        },
    }


def _verification(authority: dict[str, Any]) -> list[dict[str, Any]]:
    statement = {
        "_type": SCRIPT.IN_TOTO_STATEMENT_V1,
        "subject": [
            {
                "name": SCRIPT.BORON_REPORT_NAME,
                "digest": {"sha256": REPORT_DIGEST.removeprefix("sha256:")},
            }
        ],
        "predicateType": SCRIPT.SLSA_PROVENANCE_V1,
        "predicate": {},
    }
    return [
        {
            "attestation": {
                "bundle": {
                    "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                    "verificationMaterial": {"certificate": {"rawBytes": "Y2VydA=="}},
                    "dsseEnvelope": {
                        "payload": base64.b64encode(
                            json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
                        ).decode(),
                        "payloadType": SCRIPT.DSSE_IN_TOTO_PAYLOAD_TYPE,
                        "signatures": [{"sig": base64.b64encode(b"signature").decode()}],
                    },
                },
                "bundle_url": "https://api.github.com/example",
                "initiator": "workflow",
            },
            "verificationResult": {
                "mediaType": SCRIPT.SIGSTORE_VERIFICATION_RESULT_MEDIA_TYPE,
                "statement": statement,
                "signature": {
                    "certificate": {
                        "certificateIssuer": "CN=github-actions-sigstore",
                        "subjectAlternativeName": SCRIPT.CI_WORKFLOW_IDENTITY,
                        "buildSignerURI": SCRIPT.CI_WORKFLOW_IDENTITY,
                        "buildSignerDigest": REVISION,
                        "runnerEnvironment": "github-hosted",
                        "sourceRepositoryURI": f"https://github.com/{SCRIPT.REPOSITORY}",
                        "sourceRepositoryDigest": REVISION,
                        "sourceRepositoryRef": SCRIPT.DEFAULT_REF,
                        "sourceRepositoryIdentifier": str(SCRIPT.REPOSITORY_ID),
                        "buildTrigger": "push",
                        "runInvocationURI": (f"https://github.com/{SCRIPT.REPOSITORY}/actions/runs/501/attempts/2"),
                    }
                },
                "verifiedTimestamps": [
                    {
                        "type": "Tlog",
                        "uri": "https://rekor.sigstore.dev",
                        "timestamp": "2026-08-09T00:00:00Z",
                    }
                ],
            },
        }
    ]


def test_authority_binds_draft_tag_protected_main_run_and_artifact() -> None:
    receipt = _authority()
    assert receipt["status"] == "passed"
    assert receipt["source"] == {
        "protected": True,
        "ref": SCRIPT.DEFAULT_REF,
        "revision": REVISION,
    }
    assert receipt["boron"]["run_id"] == 501
    assert receipt["boron"]["run_attempt"] == 2
    assert receipt["boron"]["run_started_at"] == "2026-08-08T23:59:00Z"
    assert receipt["boron"]["run_completed_at"] == "2026-08-09T00:01:00Z"
    assert receipt["boron"]["artifact_id"] == 601
    assert receipt["authority_digest"].startswith("sha256:")
    assert SCRIPT._validate_authority_receipt(receipt) == receipt


def test_authority_resolves_one_annotated_tag() -> None:
    documents = _api_documents()
    documents[f"repos/{SCRIPT.REPOSITORY}/git/ref/tags/{TAG}"]["object"] = {
        "sha": "d" * 40,
        "type": "tag",
    }
    documents[f"repos/{SCRIPT.REPOSITORY}/git/tags/{'d' * 40}"] = {
        "sha": "d" * 40,
        "object": {"sha": REVISION, "type": "commit"},
    }
    assert _authority(documents)["source"]["revision"] == REVISION


def test_authority_accepts_only_a_complete_immutable_published_retry() -> None:
    documents = _api_documents()
    draft_receipt = _authority(documents)
    release = documents[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"]
    release.update(
        draft=False,
        immutable=True,
        published_at="2026-08-09T00:02:00Z",
        assets=_release_assets(),
    )
    published_receipt, state = _authority_result(documents)
    assert state == "published-exact"
    assert published_receipt == draft_receipt

    release["assets"].pop()
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_draft"):
        _authority(documents)


def test_authority_allows_only_asset_backed_ancestor_recovery_after_main_advances() -> None:
    head = "f" * 40
    documents = _api_documents()
    documents[f"repos/{SCRIPT.REPOSITORY}/branches/{SCRIPT.DEFAULT_BRANCH}"]["commit"]["sha"] = head
    documents[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"]["assets"] = _release_assets()
    documents[f"repos/{SCRIPT.REPOSITORY}/compare/{REVISION}...{head}"] = {
        "status": "ahead",
        "ahead_by": 2,
        "behind_by": 0,
        "base_commit": {"sha": REVISION},
        "merge_base_commit": {"sha": REVISION},
    }
    receipt, state = SCRIPT.validate_authority(
        tag=TAG,
        environment=_environment(GITHUB_SHA=head, GITHUB_WORKFLOW_SHA=head),
        checkout_revision=head,
        policy_receipt=_repository_policy(),
        api_get=lambda endpoint: copy.deepcopy(documents[endpoint]),
    )
    assert state == "draft-exact"
    assert receipt["source"]["revision"] == REVISION

    documents[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"]["assets"] = []
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_tag_not_default_head"):
        SCRIPT.validate_authority(
            tag=TAG,
            environment=_environment(GITHUB_SHA=head, GITHUB_WORKFLOW_SHA=head),
            checkout_revision=head,
            policy_receipt=_repository_policy(),
            api_get=lambda endpoint: copy.deepcopy(documents[endpoint]),
        )


def test_authority_rejects_a_partial_draft_asset_set_on_fresh_dispatch() -> None:
    documents = _api_documents()
    documents[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"]["assets"] = _release_assets()[:-1]
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_draft_asset_set"):
        _authority(documents)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda rows: rows[f"repos/{SCRIPT.REPOSITORY}/branches/{SCRIPT.DEFAULT_BRANCH}"].update(protected=False),
            "release_default_branch",
        ),
        (
            lambda rows: rows[f"repos/{SCRIPT.REPOSITORY}/git/ref/tags/{TAG}"]["object"].update(sha="e" * 40),
            "release_tag_not_default_head",
        ),
        (
            lambda rows: rows[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"].update(draft=False),
            "release_draft",
        ),
        (
            lambda rows: rows[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"].update(prerelease=True),
            "release_draft",
        ),
        (
            lambda rows: next(value for key, value in rows.items() if key.endswith("status=success&per_page=100"))[
                "workflow_runs"
            ][0].update(conclusion="failure"),
            "release_ci_run",
        ),
        (
            lambda rows: next(value for key, value in rows.items() if key.endswith("status=success&per_page=100"))[
                "workflow_runs"
            ][0].update(run_started_at="not-a-timestamp"),
            "release_ci_run_time",
        ),
        (
            lambda rows: next(value for key, value in rows.items() if "?name=boron-browser-boundary" in key)[
                "artifacts"
            ][0].update(expired=True),
            "release_boron_artifact",
        ),
    ],
)
def test_authority_rejects_adversarial_repository_state(mutate, reason: str) -> None:
    documents = _api_documents()
    mutate(documents)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match=reason):
        _authority(documents)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda immutable, _summaries, _details: immutable.update(enabled=False),
        lambda _immutable, summaries, _details: summaries.append(copy.deepcopy(summaries[0])),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(target="branch"),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(source="other/repository"),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(enforcement="evaluate"),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(bypass_actors=[{"actor_id": 1}]),
        lambda _immutable, _summaries, details: details[RULESET_ID]["conditions"]["ref_name"].update(
            include=["refs/tags/release-*"],
        ),
        lambda _immutable, _summaries, details: details[RULESET_ID]["conditions"]["ref_name"].update(
            exclude=["refs/tags/v0.*"],
        ),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(rules=[{"type": "update"}]),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(
            rules=[{"type": "update"}, {"type": "creation"}],
        ),
        lambda _immutable, _summaries, details: details[RULESET_ID].update(
            rules=[
                {"type": "deletion"},
                {"type": "update", "parameters": {"update_allows_fetch_and_merge": True}},
            ],
        ),
    ],
)
def test_repository_policy_rejects_missing_or_bypassable_tag_authority(mutation) -> None:
    summary = {
        "id": RULESET_ID,
        "name": "Protect public release tags",
        "source": SCRIPT.REPOSITORY,
        "enforcement": "active",
        "target": "tag",
    }
    detail = {
        **copy.deepcopy(summary),
        "source_type": "Repository",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": [SCRIPT.RELEASE_TAG_RULESET_PATTERN]}},
        "rules": [{"type": "deletion"}, {"type": "update"}],
    }
    immutable: dict[str, Any] = {"enabled": True}
    summaries = [summary]
    details = {RULESET_ID: detail}
    mutation(immutable, summaries, details)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_(?:immutability_authority|repository_policy)"):
        SCRIPT.validate_repository_policy(immutable=immutable, summaries=summaries, details=details)


def test_authority_rejects_ambiguous_run_and_unexpected_release_asset() -> None:
    documents = _api_documents()
    runs = next(value for key, value in documents.items() if key.endswith("status=success&per_page=100"))
    runs["total_count"] = 2
    runs["workflow_runs"].append(copy.deepcopy(runs["workflow_runs"][0]))
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_ci_run_count"):
        _authority(documents)

    documents = _api_documents()
    documents[f"repos/{SCRIPT.REPOSITORY}/releases/tags/{TAG}"]["assets"] = [
        {
            "id": 1,
            "name": "unreviewed.bin",
            "state": "uploaded",
            "size": 1,
            "digest": "sha256:" + "1" * 64,
        }
    ]
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_asset_name"):
        _authority(documents)


def test_dispatch_rejects_non_main_or_untrusted_workflow_revision() -> None:
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_dispatch_ref"):
        SCRIPT.DispatchContext.from_environment(_environment(GITHUB_REF="refs/heads/release"))
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_workflow_revision"):
        SCRIPT.DispatchContext.from_environment(_environment(GITHUB_WORKFLOW_SHA="f" * 40))


def test_repository_policy_loader_binds_exact_raw_snapshot(tmp_path: Path) -> None:
    summary = {
        "id": RULESET_ID,
        "name": "Protect public release tags",
        "source": SCRIPT.REPOSITORY,
        "enforcement": "active",
        "target": "tag",
    }
    detail = {
        **summary,
        "source_type": "Repository",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": [SCRIPT.RELEASE_TAG_RULESET_PATTERN]}},
        "rules": [{"type": "deletion"}, {"type": "update"}],
    }
    (tmp_path / "immutable.json").write_text('{"enabled":true}', encoding="utf-8")
    (tmp_path / "rulesets.json").write_text(json.dumps([summary]), encoding="utf-8")
    (tmp_path / f"ruleset-{RULESET_ID}.json").write_text(json.dumps(detail), encoding="utf-8")
    receipt = SCRIPT.load_repository_policy(tmp_path)
    assert receipt["tag_ruleset"]["id"] == RULESET_ID
    assert SCRIPT._validate_policy_receipt(receipt) == receipt

    (tmp_path / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_repository_policy_files"):
        SCRIPT.load_repository_policy(tmp_path)


def test_report_prebinding_requires_single_exact_same_run_file(tmp_path: Path) -> None:
    authority = _authority()
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report_path = report_dir / SCRIPT.BORON_REPORT_NAME
    payload = json.dumps(_report(authority), sort_keys=True).encode()
    report_path.write_bytes(payload)
    assert SCRIPT.verify_report(authority, report_path) == ("sha256:" + hashlib.sha256(payload).hexdigest())

    changed = _report(authority)
    changed["runner"]["run_id"] = 999
    report_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_report_binding"):
        SCRIPT.verify_report(authority, report_path)

    report_path.write_bytes(payload)
    (report_dir / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_report_directory"):
        SCRIPT.verify_report(authority, report_path)


def test_attestation_retains_exact_verified_bundle_and_run_identity() -> None:
    authority = _authority()
    verification = _verification(authority)
    bundle = SCRIPT.validated_attestation_bundle(
        authority,
        report_digest=REPORT_DIGEST,
        verification=verification,
    )
    assert json.loads(bundle) == verification[0]["attestation"]["bundle"]
    assert bundle.endswith(b"\n")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value[0]["verificationResult"]["statement"].update(
                predicateType="https://example.invalid/predicate"
            ),
            "release_attestation_subject",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"]["subject"][0].update(name="unrelated.json"),
            "release_attestation_subject",
        ),
        (
            lambda value: value[0]["verificationResult"]["signature"]["certificate"].update(
                runInvocationURI=(f"https://github.com/{SCRIPT.REPOSITORY}/actions/runs/999/attempts/2")
            ),
            "release_attestation_certificate",
        ),
        (
            lambda value: value[0]["verificationResult"].update(verifiedTimestamps=[]),
            "release_attestation_timestamp",
        ),
    ],
)
def test_attestation_rejects_wrong_subject_predicate_run_or_timestamp(mutation, reason: str) -> None:
    authority = _authority()
    verification = _verification(authority)
    mutation(verification)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match=reason):
        SCRIPT.validated_attestation_bundle(
            authority,
            report_digest=REPORT_DIGEST,
            verification=verification,
        )


def test_attestation_rejects_missing_or_multiple_verified_results() -> None:
    authority = _authority()
    for verification in ([], _verification(authority) * 2):
        with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_attestation_count"):
            SCRIPT.validated_attestation_bundle(
                authority,
                report_digest=REPORT_DIGEST,
                verification=verification,
            )


def test_attestation_rejects_bundle_statement_mismatch() -> None:
    authority = _authority()
    verification = _verification(authority)
    unrelated = copy.deepcopy(verification[0]["verificationResult"]["statement"])
    unrelated["subject"][0]["name"] = "unrelated.json"
    verification[0]["attestation"]["bundle"]["dsseEnvelope"]["payload"] = base64.b64encode(
        json.dumps(unrelated, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_attestation_bundle_statement"):
        SCRIPT.validated_attestation_bundle(
            authority,
            report_digest=REPORT_DIGEST,
            verification=verification,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(timestamp="2026-08-08T22:00:00Z"),
        lambda row: row.update(timestamp="not-rfc3339"),
        lambda row: row.update(type="CurrentTime"),
        lambda row: row.update(uri="http://rekor.sigstore.dev"),
        lambda row: row.update(extra="smuggled"),
    ],
)
def test_attestation_rejects_stale_or_malformed_verified_timestamp(mutation) -> None:
    authority = _authority()
    verification = _verification(authority)
    mutation(verification[0]["verificationResult"]["verifiedTimestamps"][0])
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_attestation_timestamp"):
        SCRIPT.validated_attestation_bundle(
            authority,
            report_digest=REPORT_DIGEST,
            verification=verification,
        )


def test_descriptor_read_rejects_symlink_and_lstat_open_swap(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "evidence.json"
    target.write_bytes(b"original")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_test_read"):
        SCRIPT._read_regular(link, maximum=64, reason_code="release_test_read")

    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"malicious")
    original_open = SCRIPT.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == target and not swapped and kwargs.get("dir_fd") is None:
            swapped = True
            os.replace(replacement, target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(SCRIPT.os, "open", racing_open)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_test_read"):
        SCRIPT._read_regular(target, maximum=64, reason_code="release_test_read")
    assert swapped is True


def test_descriptor_outputs_reject_symlinks_and_never_overwrite(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    output = tmp_path / "output"
    output.symlink_to(victim)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_output_exists"):
        SCRIPT._atomic_write(output, b"evidence\n")
    assert victim.read_bytes() == b"preserve"

    github_output = tmp_path / "github-output"
    github_output.symlink_to(victim)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_github_output"):
        SCRIPT._append_outputs(github_output, {"source-sha": REVISION})
    assert victim.read_bytes() == b"preserve"


def test_descriptor_outputs_write_exact_bytes_through_bound_directory(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    SCRIPT._atomic_write(output, b"evidence\n")
    assert output.read_bytes() == b"evidence\n"
    assert output.stat().st_mode & 0o777 == 0o600

    github_output = tmp_path / "github-output"
    github_output.write_bytes(b"")
    SCRIPT._append_outputs(github_output, {"source-sha": REVISION})
    assert github_output.read_bytes() == f"source-sha={REVISION}\n".encode()


def test_atomic_output_directory_swap_cannot_redirect_bytes(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "bound"
    parent.mkdir()
    displaced = tmp_path / "displaced"
    output = parent / "receipt.json"
    original_open = SCRIPT.os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == output.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            os.replace(parent, displaced)
            parent.mkdir()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(SCRIPT.os, "open", racing_open)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_output_write"):
        SCRIPT._atomic_write(output, b"bound evidence\n")
    assert swapped is True
    assert not output.exists()
    assert not (displaced / output.name).exists()


def test_atomic_output_removes_partial_file_after_write_or_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "receipt.json"

    def fail_write(_descriptor: int, _payload: bytes, reason_code: str) -> None:
        raise SCRIPT.ReleaseAuthorityRejected(reason_code)

    monkeypatch.setattr(SCRIPT, "_write_all", fail_write)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_output_write"):
        SCRIPT._atomic_write(output, b"bound\n")
    assert not output.exists()

    monkeypatch.undo()
    real_fsync = SCRIPT.os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected")
        real_fsync(descriptor)

    monkeypatch.setattr(SCRIPT.os, "fsync", fail_first_fsync)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_output_write"):
        SCRIPT._atomic_write(output, b"bound\n")
    assert not output.exists()


def test_atomic_output_removes_partial_file_after_post_stat_failure(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "receipt.json"
    decoy = tmp_path / "decoy"
    decoy.write_bytes(b"different identity")
    real_stat = SCRIPT.os.stat
    calls = 0

    def swap_first_post_stat(path, *args, **kwargs):
        nonlocal calls
        if path == output.name and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 1:
                return real_stat(decoy)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(SCRIPT.os, "stat", swap_first_post_stat)
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_output_write"):
        SCRIPT._atomic_write(output, b"bound\n")
    assert not output.exists()
    assert decoy.read_bytes() == b"different identity"


def _write_distributions(directory: Path) -> dict[str, bytes]:
    directory.mkdir()
    payloads = {
        "algo_cli_runtime-0.19.0-py3-none-any.whl": b"wheel",
        "algo_cli_runtime-0.19.0.tar.gz": b"sdist",
    }
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
    return payloads


def _pypi_document(payloads: dict[str, bytes]) -> bytes:
    return json.dumps(
        {
            "info": {"name": "algo-cli-runtime", "version": "0.19.0"},
            "urls": [
                {
                    "filename": name,
                    "digests": {"sha256": hashlib.sha256(payload).hexdigest()},
                    "size": len(payload),
                    "packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
                    "yanked": False,
                }
                for name, payload in sorted(payloads.items())
            ],
        },
        sort_keys=True,
    ).encode()


def test_pypi_retry_state_supports_partial_exact_and_rejects_conflicts(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    payloads = _write_distributions(dist)
    assert (
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=False,
            fetch=lambda _url: b"",
        )
        == "absent"
    )
    document = _pypi_document(payloads)
    partial = json.loads(document)
    partial["urls"] = partial["urls"][:1]
    partial_document = json.dumps(partial).encode()
    assert (
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=False,
            fetch=lambda _url: partial_document,
        )
        == "partial-exact"
    )
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_pypi_missing"):
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=True,
            fetch=lambda _url: partial_document,
        )
    assert (
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=True,
            fetch=lambda _url: document,
        )
        == "exact"
    )
    wrong = json.loads(document)
    wrong["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_pypi_digest"):
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=False,
            fetch=lambda _url: json.dumps(wrong).encode(),
        )

    extra = json.loads(document)
    extra["urls"][0]["filename"] = "unexpected.whl"
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_pypi_file"):
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=False,
            fetch=lambda _url: json.dumps(extra).encode(),
        )

    yanked = json.loads(document)
    yanked["urls"][0]["yanked"] = True
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_pypi_file"):
        SCRIPT.verify_pypi_state(
            tag=TAG,
            directory=dist,
            require_present=False,
            fetch=lambda _url: json.dumps(yanked).encode(),
        )


def _durable_asset_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    authority = SCRIPT._canonical(_authority()) + b"\n"
    policy = SCRIPT._canonical(_repository_policy()) + b"\n"
    checksum_names = {"SHA256SUMS", "grace-release-evidence-SHA256SUMS"}
    for name in SCRIPT.expected_release_assets(TAG) - checksum_names:
        if name == "oliver-release-authority.json":
            payload = authority
        elif name == "oliver-release-repository-policy.json":
            payload = policy
        elif name.endswith(".json"):
            payload = b'{"fixture":true}\n'
        else:
            payload = f"fixture:{name}\n".encode()
        (assets / name).write_bytes(payload)
    inner_checksums = "".join(
        f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(SCRIPT._inner_evidence_names(TAG))
    ).encode("ascii")
    (assets / "SHA256SUMS").write_bytes(inner_checksums)
    checksums = "".join(
        f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(SCRIPT.expected_release_assets(TAG) - {"grace-release-evidence-SHA256SUMS"})
    ).encode("ascii")
    (assets / "grace-release-evidence-SHA256SUMS").write_bytes(checksums)
    current_authority = tmp_path / "current-authority.json"
    current_policy = tmp_path / "current-policy.json"
    current_authority.write_bytes(authority)
    current_policy.write_bytes(policy)
    release = {
        "id": 301,
        "tag_name": TAG,
        "target_commitish": REVISION,
        "draft": True,
        "prerelease": False,
        "immutable": False,
        "published_at": None,
        "assets": [
            {
                "id": index,
                "name": path.name,
                "state": "uploaded",
                "size": path.stat().st_size,
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for index, path in enumerate(sorted(assets.iterdir()), start=1)
        ],
    }
    return assets, current_authority, current_policy, release


def test_durable_asset_reconciliation_binds_api_checksums_and_stable_receipts(tmp_path: Path) -> None:
    assets, authority, policy, release = _durable_asset_fixture(tmp_path)
    plan = SCRIPT.durable_asset_plan(
        tag=TAG,
        release_id=301,
        release_state="draft-exact",
        release=release,
    )
    assert plan["kind"] == "algo-cli-release-asset-plan"
    result = SCRIPT.validate_durable_assets(
        tag=TAG,
        release_id=301,
        release_state="draft-exact",
        source_revision=REVISION,
        release=release,
        directory=assets,
        authority_path=authority,
        policy_path=policy,
        report_path=assets / SCRIPT.BORON_REPORT_NAME,
        boron_bundle_path=assets / "grace-boron-hosted-qualification.sigstore.jsonl",
    )
    assert result["source_revision"] == REVISION

    published = copy.deepcopy(release)
    published.update(draft=False, immutable=True, published_at="2026-08-09T00:02:00Z")
    assert (
        SCRIPT.durable_asset_plan(
            tag=TAG,
            release_id=301,
            release_state="published-exact",
            release=published,
        )["release"]["state"]
        == "published-exact"
    )


@pytest.mark.parametrize(
    "mutated_name",
    [
        f"algo_cli_runtime-{TAG[1:]}-py3-none-any.whl",
        "algo-cli-runtime.lock.cdx.json",
    ],
)
def test_release_asset_closure_rejects_inner_evidence_mutation_even_with_fresh_outer_checksum(
    tmp_path: Path,
    mutated_name: str,
) -> None:
    assets, _authority, _policy, _release = _durable_asset_fixture(tmp_path)
    assert SCRIPT.validate_release_asset_closure(tag=TAG, directory=assets)["status"] == "passed"
    (assets / mutated_name).write_bytes(b"post-verification mutation\n")
    outer_name = "grace-release-evidence-SHA256SUMS"
    (assets / outer_name).write_text(
        "".join(
            f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(SCRIPT.expected_release_assets(TAG) - {outer_name})
        ),
        encoding="ascii",
        newline="\n",
    )
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_evidence_checksum"):
        SCRIPT.validate_release_asset_closure(tag=TAG, directory=assets)


def test_durable_reconciliation_rejects_api_bound_outer_consistent_inner_mutation(tmp_path: Path) -> None:
    assets, authority, policy, release = _durable_asset_fixture(tmp_path)
    wheel = assets / f"algo_cli_runtime-{TAG[1:]}-py3-none-any.whl"
    wheel.write_bytes(b"post-verification mutation\n")
    outer = assets / "grace-release-evidence-SHA256SUMS"
    outer.write_text(
        "".join(
            f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(SCRIPT.expected_release_assets(TAG) - {outer.name})
        ),
        encoding="ascii",
        newline="\n",
    )
    for path in (wheel, outer):
        row = next(item for item in release["assets"] if item["name"] == path.name)
        row["size"] = path.stat().st_size
        row["digest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_evidence_checksum"):
        SCRIPT.validate_durable_assets(
            tag=TAG,
            release_id=301,
            release_state="draft-exact",
            source_revision=REVISION,
            release=release,
            directory=assets,
            authority_path=authority,
            policy_path=policy,
            report_path=assets / SCRIPT.BORON_REPORT_NAME,
            boron_bundle_path=assets / "grace-boron-hosted-qualification.sigstore.jsonl",
        )


def test_durable_asset_reconciliation_rejects_digest_checksum_and_authority_tampering(tmp_path: Path) -> None:
    assets, authority, policy, release = _durable_asset_fixture(tmp_path)
    target = assets / SCRIPT.BORON_REPORT_NAME
    target.write_bytes(b"tampered\n")
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_reconcile_asset_digest"):
        SCRIPT.validate_durable_assets(
            tag=TAG,
            release_id=301,
            release_state="draft-exact",
            source_revision=REVISION,
            release=release,
            directory=assets,
            authority_path=authority,
            policy_path=policy,
            report_path=assets / SCRIPT.BORON_REPORT_NAME,
            boron_bundle_path=assets / "grace-boron-hosted-qualification.sigstore.jsonl",
        )

    assets, authority, policy, release = _durable_asset_fixture(tmp_path / "checksum")
    target = assets / SCRIPT.BORON_REPORT_NAME
    target.write_bytes(b"changed but API-bound\n")
    row = next(item for item in release["assets"] if item["name"] == target.name)
    row["size"] = target.stat().st_size
    row["digest"] = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_reconcile_checksum"):
        SCRIPT.validate_durable_assets(
            tag=TAG,
            release_id=301,
            release_state="draft-exact",
            source_revision=REVISION,
            release=release,
            directory=assets,
            authority_path=authority,
            policy_path=policy,
            report_path=assets / SCRIPT.BORON_REPORT_NAME,
            boron_bundle_path=assets / "grace-boron-hosted-qualification.sigstore.jsonl",
        )

    assets, authority, policy, release = _durable_asset_fixture(tmp_path / "authority")
    authority.write_bytes(b'{"different":true}\n')
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_reconcile_authority"):
        SCRIPT.validate_durable_assets(
            tag=TAG,
            release_id=301,
            release_state="draft-exact",
            source_revision=REVISION,
            release=release,
            directory=assets,
            authority_path=authority,
            policy_path=policy,
            report_path=assets / SCRIPT.BORON_REPORT_NAME,
            boron_bundle_path=assets / "grace-boron-hosted-qualification.sigstore.jsonl",
        )


def _release_verification_fixture(
    *, predicate_type: str, predicate: dict[str, Any], distributions: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    statement = {
        "_type": SCRIPT.IN_TOTO_STATEMENT_V1,
        "subject": [{"name": name, "digest": {"sha256": row["digest"]}} for name, row in sorted(distributions.items())],
        "predicateType": predicate_type,
        "predicate": predicate,
    }
    bundle = {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {"certificate": {"rawBytes": "Y2VydA=="}},
        "dsseEnvelope": {
            "payload": base64.b64encode(SCRIPT._canonical(statement)).decode(),
            "payloadType": SCRIPT.DSSE_IN_TOTO_PAYLOAD_TYPE,
            "signatures": [{"sig": base64.b64encode(b"signature").decode()}],
        },
    }
    verification = [
        {
            "attestation": {"bundle": bundle},
            "verificationResult": {
                "mediaType": SCRIPT.SIGSTORE_VERIFICATION_RESULT_MEDIA_TYPE,
                "statement": statement,
                "signature": {
                    "certificate": {
                        "subjectAlternativeName": SCRIPT.RELEASE_WORKFLOW_IDENTITY,
                        "buildSignerURI": SCRIPT.RELEASE_WORKFLOW_IDENTITY,
                        "buildSignerDigest": REVISION,
                        "runnerEnvironment": "github-hosted",
                        "sourceRepositoryURI": f"https://github.com/{SCRIPT.REPOSITORY}",
                        "sourceRepositoryDigest": REVISION,
                        "sourceRepositoryRef": SCRIPT.DEFAULT_REF,
                        "sourceRepositoryIdentifier": str(SCRIPT.REPOSITORY_ID),
                        "buildTrigger": "workflow_dispatch",
                        "runInvocationURI": (f"https://github.com/{SCRIPT.REPOSITORY}/actions/runs/900/attempts/1"),
                    }
                },
                "verifiedTimestamps": [
                    {
                        "type": "Tlog",
                        "uri": "https://rekor.sigstore.dev",
                        "timestamp": "2026-08-09T00:03:00Z",
                    }
                ],
            },
        }
    ]
    return verification, bundle


def test_release_bundle_verification_binds_subject_predicate_bundle_and_run(tmp_path: Path) -> None:
    distributions = {
        "algo_cli_runtime-0.19.0-py3-none-any.whl": {"digest": "a" * 64, "size": 1},
        "algo_cli_runtime-0.19.0.tar.gz": {"digest": "b" * 64, "size": 1},
    }
    predicate = {"source": REVISION}
    verification, bundle = _release_verification_fixture(
        predicate_type=SCRIPT.SOURCE_BINDING_PREDICATE,
        predicate=predicate,
        distributions=distributions,
    )
    verification_path = tmp_path / "verification.json"
    bundle_path = tmp_path / "bundle.jsonl"
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    bundle_path.write_bytes(SCRIPT._canonical(bundle) + b"\n")
    run_uri, _statement = SCRIPT._validate_release_verification(
        path=verification_path,
        bundle_path=bundle_path,
        distributions=distributions,
        predicate_type=SCRIPT.SOURCE_BINDING_PREDICATE,
        predicate=predicate,
        source_revision=REVISION,
    )
    assert run_uri.endswith("/actions/runs/900/attempts/1")

    for mutation, reason in (
        (
            lambda value: value[0]["verificationResult"]["statement"]["subject"][0]["digest"].update(sha256="f" * 64),
            "release_bundle_subject",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"].update(predicate={"source": "wrong"}),
            "release_bundle_predicate",
        ),
        (
            lambda value: value[0]["verificationResult"]["signature"]["certificate"].update(
                runInvocationURI="https://github.com/other/repo/actions/runs/900/attempts/1"
            ),
            "release_bundle_certificate",
        ),
    ):
        changed = copy.deepcopy(verification)
        mutation(changed)
        verification_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match=reason):
            SCRIPT._validate_release_verification(
                path=verification_path,
                bundle_path=bundle_path,
                distributions=distributions,
                predicate_type=SCRIPT.SOURCE_BINDING_PREDICATE,
                predicate=predicate,
                source_revision=REVISION,
            )

    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    bundle_path.write_bytes(SCRIPT._canonical({**bundle, "verificationMaterial": {"tampered": True}}) + b"\n")
    with pytest.raises(SCRIPT.ReleaseAuthorityRejected, match="release_bundle_mismatch"):
        SCRIPT._validate_release_verification(
            path=verification_path,
            bundle_path=bundle_path,
            distributions=distributions,
            predicate_type=SCRIPT.SOURCE_BINDING_PREDICATE,
            predicate=predicate,
            source_revision=REVISION,
        )


def test_release_workflow_is_draft_first_least_privilege_and_durable() -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    assert _yaml_duplicate_mapping_keys(workflow) == ()
    job_outputs = _workflow_job_outputs(workflow)
    for current_job, body in _workflow_job_bodies(workflow).items():
        needs_match = re.search(r"^    needs:\s*(.+)$", body, flags=re.MULTILINE)
        direct_needs = (
            frozenset(value.strip() for value in needs_match.group(1).strip("[]").split(","))
            if needs_match is not None
            else frozenset()
        )
        for job, output in re.findall(r"needs\.([a-z][a-z0-9-]*)\.outputs\.([a-z][a-z0-9-]*)", body):
            assert output in job_outputs[job], f"undefined needs.{job}.outputs.{output}"
            assert job in direct_needs, f"{current_job} reads non-direct needs.{job}.outputs.{output}"
    assert "workflow_dispatch:" in workflow
    assert "types: [published]" not in workflow
    assert "python -I -B -S scripts/david_hardening_gate.py --release-event" in workflow
    assert "scripts/oliver_release_authority.py dispatch" in workflow
    assert "scripts/oliver_release_authority.py policy" in workflow
    assert "scripts/oliver_release_authority.py authority" in workflow
    assert "scripts/oliver_release_authority.py report" in workflow
    assert "scripts/oliver_release_authority.py attestation" in workflow
    assert "scripts/oliver_release_authority.py pypi" in workflow
    assert workflow.count("repos/Seabass-up/Algo-cli/immutable-releases") == 6
    assert workflow.count("repos/Seabass-up/Algo-cli/rulesets?per_page=100") == 6
    assert "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1" in workflow
    assert workflow.count("permission-administration: write") == 6
    assert "environment: release-authority" in workflow
    assert "ALGO_RELEASE_AUTHORITY_READY" in workflow
    assert "gh attestation verify" in workflow
    assert "boron-attestation-local-bundle-verification.json" in workflow
    assert "grace-boron-hosted-qualification.local-bundle.sigstore.jsonl" in workflow
    assert "offline" not in workflow.lower()
    assert "cmp --silent --" in workflow
    assert "--source-digest" in workflow
    assert "--source-ref refs/heads/main" in workflow
    assert "--signer-digest" in workflow
    assert "--deny-self-hosted-runners" in workflow
    assert "grace-boron-hosted-qualification.sigstore.jsonl" in workflow
    assert "grace-release-evidence-SHA256SUMS" in workflow
    assert "create-storage-record: false" in workflow
    assert workflow.count("uses: actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6") == 4
    assert "https://algo-cli.com/attestations/release-source-binding/v1" in workflow
    assert "oliver-release-source-binding.sigstore.jsonl" in workflow
    assert "oliver-release-authority.json" in workflow
    assert "needs.pypi-preflight.outputs.pypi-state == 'partial-exact'" in workflow
    assert "skip-existing: true" in workflow

    dispatch = workflow.split("  dispatch-authority:\n", 1)[1].split("\n  environment-authority:\n", 1)[0]
    dispatch_header = dispatch.split("    steps:\n", 1)[0]
    assert "\n    if:" not in dispatch_header
    assert "ref: ${{ github.sha }}" in dispatch
    assert dispatch.index("release_dispatch_authority") < dispatch.index("actions/checkout")
    assert dispatch.index("scripts/oliver_release_authority.py dispatch") < dispatch.index(
        "scripts/david_hardening_gate.py --release-event"
    )

    environment = workflow.split("  environment-authority:\n", 1)[1].split("\n  repository-policy:\n", 1)[0]
    assert "actions/checkout" not in environment
    assert "secrets." not in environment
    assert '"protected_branches": True' in environment
    assert 'environment.get("can_admins_bypass") is not False' in environment
    assert 'rule.get("prevent_self_review") is not True' in environment

    policy = workflow.split("  repository-policy:\n", 1)[1].split("\n  release-authority:\n", 1)[0]
    assert "actions/checkout" not in policy
    assert "contents: write" not in policy
    assert "id-token: write" not in policy
    assert "--method POST" not in policy and "--method PATCH" not in policy and "--method DELETE" not in policy
    assert "ALGO_RELEASE_POLICY_APP_PRIVATE_KEY" in policy

    authority = workflow.split("  release-authority:\n", 1)[1].split("\n  durable-reconcile:\n", 1)[0]
    assert "ref: ${{ needs.dispatch-authority.outputs.source-sha }}" in authority
    assert authority.index("scripts/oliver_release_authority.py authority") < authority.index(
        "Download only the selected attempt-scoped Boron artifact"
    )

    durable = workflow.split("  durable-reconcile:\n", 1)[1].split("\n  source-capture:\n", 1)[0]
    assert (
        "release-state == 'draft-exact' || needs.release-authority.outputs.release-state == 'published-exact'"
        in durable
    )
    assert "attestations: read" in durable and "contents: read" in durable
    assert "contents: write" not in durable and "id-token: write" not in durable
    assert "scripts/oliver_release_authority.py asset-plan" in durable
    assert "scripts/oliver_release_authority.py reconcile" in durable
    assert "scripts/oliver_release_source_binding.py verify-bound" in durable
    assert "scripts/oliver_release_authority.py release-attestations" in durable
    assert "--require-present" in durable
    assert durable.count("if: ${{ needs.release-authority.outputs.release-state == 'draft-exact' }}") == 3
    assert "if: ${{ needs.release-authority.outputs.release-state == 'published-exact' }}" in durable

    source = workflow.split("  source-capture:\n", 1)[1].split("\n  package:\n", 1)[0]
    assert "GIT_NO_REPLACE_OBJECTS=1" in source
    assert "scripts/oliver_release_source_binding.py capture" in source
    assert "ref: ${{ needs.release-authority.outputs.source-sha }}" in source
    assert '--expected-revision "${SOURCE_SHA}"' in source

    package = workflow.split("  package:\n", 1)[1].split("\n  verify:\n", 1)[0]
    assert "actions/checkout" not in package
    assert package.count(" materialize ") == 2
    assert package.count(" -m build --no-isolation ") == 2
    assert "--no-install-project --extra release" in package
    assert "--rebuild-stage" in package and "--rebuild-dist" in package
    assert '--bound-dist "${RUNNER_TEMP}/bound-dist"' in package
    assert '"${RUNNER_TEMP}"/dist-a/* "${package}' not in package
    assert '"${RUNNER_TEMP}"/dist-b/* "${package}' not in package
    assert "--report" not in package

    verify = workflow.split("  verify:\n", 1)[1].split("\n  evidence:\n", 1)[0]
    assert "scripts/oliver_release_source_binding.py verify-bound" in verify
    assert "/package/bound-dist" in verify
    assert "/package/dist-a" not in verify and "/package/dist-b" not in verify
    assert "SHA256SUMS" not in verify
    assert "algo-cli-runtime.lock.cdx.json" not in verify

    evidence = workflow.split("  evidence:\n", 1)[1].split("\n  attest:\n", 1)[0]
    assert "scripts/oliver_release_source_binding.py verify-bound" in evidence
    assert "needs.package.outputs.artifact-id" in evidence
    assert "pytest" not in evidence and "npm " not in evidence
    assert "algo-cli-runtime.lock.cdx.json" in evidence
    assert "SHA256SUMS" not in evidence

    attest = workflow.split("  attest:\n", 1)[1].split("\n  attestation-verification:\n", 1)[0]
    assert "id-token: write" in attest
    assert "attestations: write" in attest
    assert "actions/checkout" not in attest
    assert "run:" not in attest
    assert "contents: write" not in attest
    assert "needs.evidence.outputs.artifact-id" in attest

    attestation_verification = workflow.split("  attestation-verification:\n", 1)[1].split("\n  attach-evidence:\n", 1)[
        0
    ]
    assert "attestations: read" in attestation_verification
    assert "contents: read" in attestation_verification
    assert "id-token: write" not in attestation_verification
    assert "contents: write" not in attestation_verification
    assert "actions/attest@" not in attestation_verification
    assert "gh attestation verify" in attestation_verification
    assert "scripts/oliver_release_authority.py release-attestations" in attestation_verification
    assert "scripts/oliver_release_authority.py asset-closure" in attestation_verification
    assert "needs.package.outputs.artifact-id" in attestation_verification
    assert "needs.evidence.outputs.artifact-id" in attestation_verification
    assert attestation_verification.count("needs.attest.outputs.") == 4
    assert "grace-release-evidence-SHA256SUMS" in attestation_verification

    attach = workflow.split("  attach-evidence:\n", 1)[1].split("\n  release-assets-ready:\n", 1)[0]
    assert "contents: write" in attach
    assert "id-token: write" not in attach
    assert "actions/checkout" not in attach
    assert "--clobber" not in attach
    assert "release asset collision" in attach
    assert attach.count("uses: actions/download-artifact@") == 1
    assert "needs.attestation-verification.outputs.artifact-id" in attach
    assert "needs.package.outputs" not in attach
    assert "needs.verify.outputs" not in attach
    assert "inner release evidence checksum mismatch" in attach
    assert "outer release evidence checksum mismatch" in attach

    publish = workflow.split("  publish:\n", 1)[1].split("\n  pypi-verify:\n", 1)[0]
    assert "id-token: write" in publish
    assert "actions/checkout" not in publish
    assert "name: release-authority" in publish
    assert "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1" in publish
    assert "release_pre_pypi_authority" in publish
    assert "https://api.github.com/installation/token" in publish
    assert '"${status}" == "204"' in publish
    assert '"${GITHUB_RUN_ATTEMPT}" == "1"' in publish
    assert (
        "artifact-ids: ${{ needs.release-assets-ready.outputs.assets-artifact-id }}\n"
        "          path: ${{ runner.temp }}/verified-release-assets"
    ) in publish
    assert publish.index("path: ${{ runner.temp }}/verified-release-assets") < publish.index(
        'ASSETS="${RUNNER_TEMP}/verified-release-assets"'
    )
    assert 'source_get "repos/Seabass-up/Algo-cli/releases/${RELEASE_ID}"' in publish
    assert "if remote != local:" in publish
    assert "https://pypi.org/pypi/algo-cli-runtime/${version}/json" in publish
    assert "steps.immediate-authority.outputs.publish-required == 'true'" in publish
    assert publish.index("revoke_token || fail") < publish.index("pypa/gh-action-pypi-publish")
    assert "packages-dir: ${{ runner.temp }}/package/bound-dist" in publish

    pre_publish_policy = workflow.split("  repository-policy-publish:\n", 1)[1].split("\n  pypi-preflight:\n", 1)[0]
    assert "actions/checkout" not in pre_publish_policy
    assert "contents: write" not in pre_publish_policy
    assert "diff --no-dereference --recursive --brief" in pre_publish_policy
    assert "repository-policy-publish" in workflow.split("  pypi-preflight:\n", 1)[1].split("\n  publish:\n", 1)[0]

    final_policy = workflow.split("  repository-policy-final:\n", 1)[1].split("\n  publish-release:\n", 1)[0]
    assert "actions/checkout" not in final_policy
    assert "contents: write" not in final_policy
    assert "diff --no-dereference --recursive --brief" in final_policy

    finalize = workflow.split("  publish-release:\n", 1)[1].split("\n  repository-policy-postcheck:\n", 1)[0]
    assert "contents: write" in finalize
    assert "id-token: write" not in finalize
    assert "actions/checkout" not in finalize
    assert "environment: release-authority" in finalize
    assert "release_pre_publish_authority" in finalize
    assert "https://api.github.com/installation/token" in finalize
    assert finalize.index("revoke_token || fail") < finalize.index("gh api --method PATCH")
    assert '"${GITHUB_RUN_ATTEMPT}" == "1"' in finalize
    assert 'expected_state in {"draft", "draft-exact"}' in finalize
    assert "https://pypi.org/pypi/algo-cli-runtime/${version}/json" in finalize
    assert "if observed != expected:" in finalize
    assert '\'{"draft":false,"prerelease":false}\'' in finalize
    assert 'release.get("immutable") is not True' in finalize
    assert "Build and inspect reproducible distributions" not in workflow
    assert "Build and inspect source-date-pinned distributions" not in workflow
    assert "Build two source-date-pinned distributions without isolation or live resolution" in workflow
    assert finalize.count("resolve_tag") >= 2
    publish_request = finalize.index("gh api --method PATCH")
    assert finalize.index("verify_pypi before") < publish_request
    assert finalize.index("snapshot after", publish_request) > publish_request
    assert finalize.index("verify_pypi after", publish_request) > finalize.index("snapshot after", publish_request)
    assert "repos/Seabass-up/Algo-cli/commits/${RELEASE_TAG}" not in finalize
    assert "repos/Seabass-up/Algo-cli/git/ref/tags/${RELEASE_TAG}" in finalize
    assert "tag_sha != source_sha" in finalize
    assert "compare/${SOURCE_SHA}...${branch_sha}" in finalize
    assert '"${RELEASE_STATE}" == "published-exact"' in workflow
    upload_blocks = workflow.split("uses: actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4")
    assert all("\n        with:\n" in block[:300] for block in upload_blocks[1:])
    assert "overwrite:" not in workflow
    assert "name: python-package-distributions" not in workflow
    assert workflow.count("artifact-ids:") == workflow.count("uses: actions/download-artifact@")
    assert workflow.count("uses: actions/upload-artifact@") == 13
    assert workflow.count("${{ github.run_id }}-${{ github.run_attempt }}") == 13
    assert "skip-token-revoke:" not in workflow
    assert "ALGO_RELEASE_POLICY_APP_PRIVATE_KEY" not in attach
    assert "POLICY_TOKEN" not in attach
    for mutation_job in (publish, finalize):
        assert "actions/checkout" not in mutation_job
        assert mutation_job.index("unset POLICY_TOKEN") < mutation_job.index(
            "pypa/gh-action-pypi-publish" if mutation_job is publish else "gh api --method PATCH"
        )
    assert workflow.index("attach-evidence:") < workflow.index("publish:")
    assert workflow.index("publish:") < workflow.index("publish-release:")
    post_policy = workflow.split("  repository-policy-postcheck:\n", 1)[1]
    assert "needs: [repository-policy, publish-release]" in post_policy
    assert "environment: release-authority" in post_policy
    assert "permissions: {}" in post_policy
    assert "actions/checkout" not in post_policy
    assert "contents: write" not in post_policy and "id-token: write" not in post_policy
    assert "repos/Seabass-up/Algo-cli/immutable-releases" in post_policy
    assert "repos/Seabass-up/Algo-cli/rulesets?per_page=100" in post_policy
    assert "diff --no-dereference --recursive --brief" in post_policy
    assert "revoke_token || fail" in post_policy
    assert "gh api --method PATCH" not in post_policy


def test_published_exact_branch_is_read_only_and_draft_recovery_reuses_durable_bytes() -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    durable = workflow.split("  durable-reconcile:\n", 1)[1].split("\n  source-capture:\n", 1)[0]
    assert "release-state == 'published-exact'" in durable
    assert "contents: write" not in durable
    assert "id-token: write" not in durable
    assert "pypa/gh-action-pypi-publish" not in durable
    assert "gh api --method PATCH" not in durable
    assert "gh release upload" not in durable
    assert durable.count("uses: actions/upload-artifact@") == 2
    assert durable.count("if: ${{ needs.release-authority.outputs.release-state == 'draft-exact' }}") == 3

    for job, next_job in (
        ("source-capture", "package"),
        ("package", "verify"),
        ("verify", "evidence"),
        ("evidence", "attest"),
        ("attest", "attestation-verification"),
        ("attestation-verification", "attach-evidence"),
        ("attach-evidence", "release-assets-ready"),
    ):
        body = workflow.split(f"  {job}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]
        assert "release-state == 'draft'" in body
    ready = workflow.split("  release-assets-ready:\n", 1)[1].split("\n  repository-policy-publish:\n", 1)[0]
    assert "release-state != 'published-exact'" in ready
    assert '"${RELEASE_STATE}" == "draft-exact"' in ready
    assert "RECOVERED_PACKAGE_ID" in ready and "RECOVERED_ASSETS_ID" in ready


def test_immediate_pre_pypi_asset_validator_rejects_missing_or_conflicting_assets(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    validator = _pre_pypi_asset_validator(workflow)
    assets, _authority, _policy, release = _durable_asset_fixture(tmp_path)
    package = tmp_path / "package"
    package.mkdir()
    for name in SCRIPT.expected_release_assets(TAG):
        if name.endswith((".whl", ".tar.gz")):
            (package / name).write_bytes((assets / name).read_bytes())
    state = tmp_path / "state"
    state.mkdir()
    (state / "branch.json").write_text(json.dumps({"protected": True, "commit": {"sha": REVISION}}), encoding="utf-8")
    (state / "tag.json").write_text(json.dumps({"sha": REVISION}), encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "ASSETS": str(assets),
            "PACKAGE": str(package),
            "RELEASE_ID": "301",
            "RELEASE_TAG": TAG,
            "SOURCE_SHA": REVISION,
            "STATE": str(state),
        }
    )

    def validate(document: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        (state / "release.json").write_text(json.dumps(document), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-"],
            input=validator,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    assert validate(release).returncode == 0
    missing = copy.deepcopy(release)
    missing["assets"].pop()
    assert validate(missing).returncode != 0
    conflicting = copy.deepcopy(release)
    conflicting["assets"][0]["digest"] = "sha256:" + "f" * 64
    assert validate(conflicting).returncode != 0
    (package / next(name for name in SCRIPT.expected_release_assets(TAG) if name.endswith(".whl"))).unlink()
    assert validate(release).returncode != 0


def test_immediate_pypi_validator_handles_exact_partial_absent_and_yanked(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    validator = _immediate_pypi_validator(workflow)
    dist = tmp_path / "dist"
    state = tmp_path / "state"
    dist.mkdir()
    state.mkdir()
    payloads = {
        "algo_cli_runtime-0.19.0-py3-none-any.whl": b"wheel\n",
        "algo_cli_runtime-0.19.0.tar.gz": b"sdist\n",
    }
    for name, payload in payloads.items():
        (dist / name).write_bytes(payload)
    environment = os.environ.copy()
    environment.update({"DIST": str(dist), "HTTP_STATUS": "200", "RELEASE_TAG": TAG, "STATE": str(state)})

    def validate(document: bytes, *, status: str = "200") -> subprocess.CompletedProcess[str]:
        (state / "pypi.json").write_bytes(document)
        current = dict(environment, HTTP_STATUS=status)
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-"],
            input=validator,
            env=current,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    exact = _pypi_document(payloads)
    passed = validate(exact)
    assert passed.returncode == 0 and passed.stdout == "publish-required=false\n"
    partial = json.loads(exact)
    partial["urls"].pop()
    passed = validate(json.dumps(partial).encode())
    assert passed.returncode == 0 and passed.stdout == "publish-required=true\n"
    absent = validate(b"not found", status="404")
    assert absent.returncode == 0 and absent.stdout == "publish-required=true\n"
    yanked = json.loads(exact)
    yanked["urls"][0]["yanked"] = True
    assert validate(json.dumps(yanked).encode()).returncode != 0
    duplicate = exact.replace(b'{"info":', b'{"info":{"name":"wrong"},"info":', 1)
    assert validate(duplicate).returncode != 0


def test_final_before_and_after_pypi_validator_rejects_every_nonexact_index_shape(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    validator = _final_pypi_validator(workflow)
    stage = tmp_path / "stage"
    stage.mkdir()
    payloads = {
        "algo_cli_runtime-0.19.0-py3-none-any.whl": b"wheel\n",
        "algo_cli_runtime-0.19.0.tar.gz": b"sdist\n",
    }
    for name, payload in payloads.items():
        (stage / name).write_bytes(payload)
    pypi = tmp_path / "pypi.json"
    environment = os.environ.copy()
    environment.update({"PYPI": str(pypi), "RELEASE_TAG": TAG, "STAGE": str(stage)})

    def validate(document: bytes) -> subprocess.CompletedProcess[str]:
        pypi.write_bytes(document)
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-"],
            input=validator,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    exact = _pypi_document(payloads)
    assert validate(exact).returncode == 0
    missing = json.loads(exact)
    missing["urls"].pop()
    assert validate(json.dumps(missing).encode()).returncode != 0
    duplicate = exact.replace(b'{"info":', b'{"info":{"name":"wrong"},"info":', 1)
    assert validate(duplicate).returncode != 0
    for field, value in (
        ("yanked", True),
        ("size", 999),
        ("packagetype", "sdist"),
    ):
        changed = json.loads(exact)
        changed["urls"][0][field] = value
        assert validate(json.dumps(changed).encode()).returncode != 0
    wrong_digest = json.loads(exact)
    wrong_digest["urls"][0]["digests"]["sha256"] = "f" * 64
    assert validate(json.dumps(wrong_digest).encode()).returncode != 0


def test_release_workflow_duplicate_key_parser_rejects_shadowed_action_input() -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    needle = "          path: ${{ runner.temp }}/release-authority-bundle\n"
    assert workflow.count(needle) == 1
    shadowed = workflow.replace(needle, needle + "          path: ${{ runner.temp }}/shadowed\n", 1)
    duplicates = _yaml_duplicate_mapping_keys(shadowed)
    assert len(duplicates) == 1
    assert duplicates[0][1] == "path"


def test_public_release_checklist_is_manual_draft_first_and_names_external_blockers() -> None:
    checklist = (ROOT / "docs/william-public-release-checklist.md").read_text(encoding="utf-8")
    for expected in (
        "GitHub immutable releases",
        "`refs/tags/v*`",
        "no bypass actors",
        "`release-authority`",
        "`ALGO_RELEASE_AUTHORITY_READY`",
        "`ALGO_RELEASE_POLICY_APP_CLIENT_ID`",
        "`ALGO_RELEASE_POLICY_APP_PRIVATE_KEY`",
        "exact non-prerelease",
        "**draft** release",
        "manually dispatch `Publish release`",
        "publishes the",
        "immutable GitHub release last",
        "**Re-run all jobs**",
        "`draft-exact`",
        "fresh manual",
        "explicitly revokes",
        "external-authority race boundary",
        "cannot be rolled back",
        "bundle-local",
        "not described as offline",
        "hatchling==1.31.0",
        "`--no-isolation`",
    ):
        assert expected in checklist
    assert "then publish a\n  final" not in checklist


def test_exact_environment_authority_rejects_self_review_or_admin_bypass(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    validator = _environment_authority_validator(workflow)
    state = tmp_path / "state"
    state.mkdir()
    valid_environment = {
        "name": "release-authority",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"id": 202}}],
            },
            {"type": "branch_policy"},
        ],
    }
    readiness = {
        "name": "ALGO_RELEASE_AUTHORITY_READY",
        "value": "true",
        "updated_at": "2026-08-09T00:00:00Z",
    }
    environment = os.environ.copy()
    environment.update({"STATE": str(state), "ACTOR_ID": "101"})

    def validate(document: dict[str, Any], marker: dict[str, Any] = readiness) -> subprocess.CompletedProcess[str]:
        (state / "environment.json").write_text(json.dumps(document), encoding="utf-8")
        (state / "readiness.json").write_text(json.dumps(marker), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-"],
            input=validator,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    assert validate(valid_environment).returncode == 0
    bypass = copy.deepcopy(valid_environment)
    bypass["can_admins_bypass"] = True
    assert validate(bypass).returncode != 0
    self_review = copy.deepcopy(valid_environment)
    self_review["protection_rules"][0]["reviewers"][0]["reviewer"]["id"] = 101
    assert validate(self_review).returncode != 0
    missing_branch_policy = copy.deepcopy(valid_environment)
    missing_branch_policy["protection_rules"].pop()
    assert validate(missing_branch_policy).returncode != 0
    duplicate_branch_policy = copy.deepcopy(valid_environment)
    duplicate_branch_policy["protection_rules"].append({"type": "branch_policy"})
    assert validate(duplicate_branch_policy).returncode != 0
    duplicate_reviewers = copy.deepcopy(valid_environment)
    duplicate_reviewers["protection_rules"].append(copy.deepcopy(duplicate_reviewers["protection_rules"][0]))
    assert validate(duplicate_reviewers).returncode != 0
    unexpected_rule = copy.deepcopy(valid_environment)
    unexpected_rule["protection_rules"][1] = {"type": "wait_timer", "wait_timer": 1}
    assert validate(unexpected_rule).returncode != 0
    not_ready = dict(readiness, value="false")
    assert validate(valid_environment, not_ready).returncode != 0


def test_exact_post_publish_validator_rejects_tag_move(tmp_path: Path) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    validator = _post_publish_validator(workflow)
    stage = tmp_path / "stage"
    state = tmp_path / "state"
    stage.mkdir()
    state.mkdir()
    asset = stage / "evidence.json"
    asset.write_bytes(b"bound release evidence\n")
    (state / "after-branch.json").write_text(
        json.dumps({"protected": True, "commit": {"sha": REVISION}}),
        encoding="utf-8",
    )
    (state / "after-release.json").write_text(
        json.dumps(
            {
                "id": 301,
                "tag_name": TAG,
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "published_at": "2026-08-09T00:02:00Z",
                "assets": [
                    {
                        "name": asset.name,
                        "state": "uploaded",
                        "digest": "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "size": asset.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update({"STAGE": str(stage), "STATE": str(state)})

    def validate(tag_sha: str) -> subprocess.CompletedProcess[str]:
        (state / "after-tag.sha").write_text(tag_sha + "\n", encoding="ascii")
        return subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-", TAG, "301", REVISION, "after"],
            input=validator,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    assert validate(REVISION).returncode == 0
    moved = validate("f" * 40)
    assert moved.returncode != 0
    assert "published release authority changed" in moved.stderr
