from __future__ import annotations

import base64
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_github_hardening_readiness.py"
SPEC = importlib.util.spec_from_file_location(
    "henry_github_hardening_readiness_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)

REPOSITORY = "Algo-CLI-Org/Algo-cli"


def _snapshot() -> dict[str, object]:
    workflow_files = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in sorted((ROOT / ".github" / "workflows").iterdir())
    }
    return {
        "repository": {
            "default_branch": "main",
            "full_name": REPOSITORY,
            "id": SCRIPT.EXPECTED_REPOSITORY_ID,
            "owner": {"login": "Algo-CLI-Org", "type": "Organization"},
            "visibility": "public",
        },
        "branch": {"name": "main", "protected": True},
        "environment": {
            "can_admins_bypass": False,
            "deployment_branch_policy": {
                "custom_branch_policies": False,
                "protected_branches": True,
            },
            "name": SCRIPT.ENVIRONMENT_NAME,
            "protection_rules": [
                {
                    "prevent_self_review": True,
                    "reviewers": [{"type": "User"}],
                    "type": "required_reviewers",
                }
            ],
        },
        "environment_secrets": {
            "secrets": [{"name": name} for name in sorted(SCRIPT.REQUIRED_SECRETS)],
            "total_count": len(SCRIPT.REQUIRED_SECRETS),
        },
        "runners": {
            "runners": [
                {
                    "busy": False,
                    "ephemeral": True,
                    "labels": [{"name": name} for name in sorted(SCRIPT.REQUIRED_RUNNER_LABELS)],
                    "status": "online",
                }
            ],
            "total_count": 1,
        },
        "runner_group": {
            "allows_public_repositories": True,
            "default": False,
            "id": 17,
            "name": SCRIPT.RUNNER_GROUP_NAME,
            "restricted_to_workflows": True,
            "selected_workflows": [
                f"{REPOSITORY}/.github/workflows/henry-austin-signing-qualification.yml@refs/heads/main"
            ],
            "visibility": "selected",
        },
        "runner_group_repositories": {
            "repositories": [
                {"full_name": REPOSITORY, "id": SCRIPT.EXPECTED_REPOSITORY_ID}
            ],
            "total_count": 1,
        },
        "workflows": {
            "total_count": len(workflow_files) + len(SCRIPT._PLATFORM_WORKFLOW_PATHS),
            "workflows": [
                *[{"path": path, "state": "active"} for path in sorted(workflow_files)],
                *[{"path": path, "state": "active"} for path in sorted(SCRIPT._PLATFORM_WORKFLOW_PATHS)],
            ],
        },
        "remote_workflow_files": {
            path: {
                "content": base64.b64encode(payload).decode("ascii"),
                "encoding": "base64",
                "path": path,
            }
            for path, payload in workflow_files.items()
        },
    }


def _report(
    snapshot: dict[str, object],
    *,
    repository: str = REPOSITORY,
) -> dict[str, object]:
    return SCRIPT.assess_snapshot(
        snapshot,
        repository=repository,
        expected_workflow=ROOT / SCRIPT.WORKFLOW_PATH,
    )


def _check(report: dict[str, object], check_id: str) -> dict[str, object]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return next(check for check in checks if check["id"] == check_id)


def test_exact_protected_ephemeral_remote_state_passes() -> None:
    report = _report(_snapshot())

    assert report["status"] == "passed"
    assert report["blocked_count"] == 0
    assert report["public_claim_eligible"] is False
    assert str(report["expected_workflow_digest"]).startswith("sha256:")
    assert _check(report, "protected_signing_trust_contract")["observed"] == {
        "contract_valid": True,
        "remote_workflow_present": True,
    }


def test_absent_remote_signing_workflow_is_not_misreported_as_caller_control() -> None:
    snapshot = _snapshot()
    remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
    del remote[SCRIPT.WORKFLOW_PATH]

    check = _check(_report(snapshot), "protected_signing_trust_contract")

    assert check["status"] == "blocked"
    assert check["observed"] == {
        "contract_valid": False,
        "remote_workflow_present": False,
    }


@pytest.mark.parametrize(
    ("case", "check_id"),
    [
        ("unprotected_branch", "default_branch_protected"),
        ("wrong_repository_id", "pinned_repository_identity"),
        ("personal_owner", "organization_owned_repository"),
        ("environment_missing", "native_hardening_environment"),
        ("admin_bypass", "environment_non_bypassable"),
        ("custom_branch", "environment_protected_branches_only"),
        ("self_review", "independent_environment_approval"),
        ("missing_secret", "minimal_environment_secret_inventory"),
        ("extra_secret", "minimal_environment_secret_inventory"),
        ("runner_group_unrestricted", "workflow_restricted_signing_runner_group"),
        ("runner_group_wrong_workflow", "workflow_restricted_signing_runner_group"),
        ("runner_group_wrong_repository_id", "workflow_restricted_signing_runner_group"),
        ("persistent_runner", "single_ephemeral_signing_runner"),
        ("busy_runner", "single_ephemeral_signing_runner"),
        ("legacy_runner", "single_ephemeral_signing_runner"),
        ("workflow_disabled", "signing_workflow_registered"),
        ("workflow_substituted", "signing_workflow_source_identity"),
        ("workflow_inventory_substituted", "workflow_source_inventory_identity"),
        ("alternate_signer_target", "exclusive_signing_workflow_authority"),
        ("alternate_dynamic_runner", "exclusive_signing_workflow_authority"),
        ("alternate_dynamic_environment", "exclusive_signing_workflow_authority"),
        ("caller_controlled_anchor", "protected_signing_trust_contract"),
        ("unknown_platform_workflow", "platform_workflow_inventory"),
    ],
)
def test_remote_control_plane_weaknesses_remain_blocked(
    case: str,
    check_id: str,
) -> None:
    snapshot = deepcopy(_snapshot())
    if case == "unprotected_branch":
        snapshot["branch"]["protected"] = False  # type: ignore[index]
    elif case == "wrong_repository_id":
        snapshot["repository"]["id"] = 1  # type: ignore[index]
    elif case == "personal_owner":
        snapshot["repository"]["owner"]["type"] = "User"  # type: ignore[index]
        snapshot["runner_group"] = None
        snapshot["runner_group_repositories"] = {
            "repositories": [],
            "total_count": 0,
        }
        snapshot["runners"] = {"runners": [], "total_count": 0}
    elif case == "environment_missing":
        snapshot["environment"] = None
    elif case == "admin_bypass":
        snapshot["environment"]["can_admins_bypass"] = True  # type: ignore[index]
    elif case == "custom_branch":
        policy = snapshot["environment"]["deployment_branch_policy"]  # type: ignore[index]
        policy["protected_branches"] = False
        policy["custom_branch_policies"] = True
    elif case == "self_review":
        rules = snapshot["environment"]["protection_rules"]  # type: ignore[index]
        rules[0]["prevent_self_review"] = False
    elif case == "missing_secret":
        secrets = snapshot["environment_secrets"]  # type: ignore[assignment]
        secrets["secrets"] = secrets["secrets"][:-1]
        secrets["total_count"] -= 1
    elif case == "extra_secret":
        secrets = snapshot["environment_secrets"]  # type: ignore[assignment]
        secrets["secrets"].append({"name": "UNEXPECTED_SECRET"})
        secrets["total_count"] += 1
    elif case == "runner_group_unrestricted":
        snapshot["runner_group"]["restricted_to_workflows"] = False  # type: ignore[index]
    elif case == "runner_group_wrong_workflow":
        snapshot["runner_group"]["selected_workflows"] = [  # type: ignore[index]
            f"{REPOSITORY}/.github/workflows/oliver-release.yml@refs/heads/main"
        ]
    elif case == "runner_group_wrong_repository_id":
        snapshot["runner_group_repositories"]["repositories"][0]["id"] = 1  # type: ignore[index]
    elif case == "persistent_runner":
        snapshot["runners"]["runners"][0]["ephemeral"] = False  # type: ignore[index]
    elif case == "busy_runner":
        snapshot["runners"]["runners"][0]["busy"] = True  # type: ignore[index]
    elif case == "legacy_runner":
        labels = snapshot["runners"]["runners"][0]["labels"]  # type: ignore[index]
        labels.append({"name": SCRIPT.LEGACY_RUNNER_LABEL})
    elif case == "workflow_disabled":
        workflows = snapshot["workflows"]["workflows"]  # type: ignore[index]
        target = next(row for row in workflows if row["path"] == SCRIPT.WORKFLOW_PATH)
        target["state"] = "disabled_manually"
    elif case == "workflow_substituted":
        remote = snapshot["remote_workflow_files"][SCRIPT.WORKFLOW_PATH]  # type: ignore[index]
        remote["content"] = base64.b64encode(b"substituted\n").decode("ascii")
    elif case == "workflow_inventory_substituted":
        remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
        alternate_path = ".github/workflows/oliver-ci.yml"
        remote[alternate_path]["content"] = base64.b64encode(b"name: substituted\n").decode("ascii")
    elif case == "alternate_signer_target":
        remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
        alternate_path = ".github/workflows/oliver-ci.yml"
        remote[alternate_path]["content"] = base64.b64encode(
            b"name: hostile\njobs:\n  steal:\n    runs-on: [self-hosted, algo-cli-signing-ephemeral]\n"
        ).decode("ascii")
    elif case == "alternate_dynamic_runner":
        remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
        alternate_path = ".github/workflows/oliver-ci.yml"
        remote[alternate_path]["content"] = base64.b64encode(
            b"name: hostile\njobs:\n  steal:\n    runs-on:\n      group: ${{ vars.RUNNER_GROUP }}\n"
        ).decode("ascii")
    elif case == "alternate_dynamic_environment":
        remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
        alternate_path = ".github/workflows/oliver-ci.yml"
        remote[alternate_path]["content"] = base64.b64encode(
            b"name: hostile\njobs:\n  steal:\n    runs-on: ubuntu-latest\n"
            b"    environment:\n      name: ${{ vars.ENVIRONMENT }}\n"
        ).decode("ascii")
    elif case == "caller_controlled_anchor":
        remote = snapshot["remote_workflow_files"]  # type: ignore[assignment]
        target = remote[SCRIPT.WORKFLOW_PATH]
        payload = base64.b64decode(target["content"])
        payload = payload.replace(
            b"secrets.AUSTIN_TEAM_ID",
            b"inputs.team_id",
            1,
        )
        target["content"] = base64.b64encode(payload).decode("ascii")
    elif case == "unknown_platform_workflow":
        workflows = snapshot["workflows"]  # type: ignore[assignment]
        workflows["workflows"].append({"path": "dynamic/unknown/platform-job", "state": "active"})
        workflows["total_count"] += 1
    else:  # pragma: no cover - protects the table itself
        raise AssertionError(case)

    report = _report(snapshot)

    assert report["status"] == "blocked"
    assert _check(report, check_id)["status"] == "blocked"
    if case == "caller_controlled_anchor":
        assert _check(report, check_id)["observed"] == {
            "contract_valid": False,
            "remote_workflow_present": True,
        }


def test_unrelated_github_hosted_secret_is_not_signing_authority() -> None:
    signing = (ROOT / SCRIPT.WORKFLOW_PATH).read_bytes()
    unrelated = (
        b"name: unrelated\njobs:\n  check:\n    runs-on: ubuntu-latest\n"
        b"    steps:\n      - run: tool\n"
        b"        env:\n          UNRELATED_TOKEN: ${{ secrets.UNRELATED_TOKEN }}\n"
    )

    assert SCRIPT._exclusive_signing_authority(
        {
            SCRIPT.WORKFLOW_PATH: signing,
            ".github/workflows/unrelated.yml": unrelated,
        }
    )


def test_runner_label_ambiguity_and_api_pagination_reject_evidence() -> None:
    duplicate = _snapshot()
    labels = duplicate["runners"]["runners"][0]["labels"]  # type: ignore[index]
    labels.append(deepcopy(labels[0]))
    with pytest.raises(
        SCRIPT.GitHubHardeningReadinessRejected,
        match="github_readiness_runner",
    ):
        _report(duplicate)

    paginated = _snapshot()
    paginated["workflows"]["total_count"] = 2  # type: ignore[index]
    with pytest.raises(
        SCRIPT.GitHubHardeningReadinessRejected,
        match="github_readiness_api_pagination",
    ):
        _report(paginated)


def test_noncanonical_or_oversized_identity_inputs_reject() -> None:
    malformed = _snapshot()
    malformed["repository"]["default_branch"] = "main..substitute"  # type: ignore[index]
    with pytest.raises(
        SCRIPT.GitHubHardeningReadinessRejected,
        match="github_readiness_ref",
    ):
        _report(malformed)

    with pytest.raises(
        SCRIPT.GitHubHardeningReadinessRejected,
        match="github_readiness_repository",
    ):
        SCRIPT.collect_snapshot(repository="owner/../repo", gh_binary="gh")


def test_same_pinned_repository_id_accepts_a_transferred_owner_and_name() -> None:
    snapshot = _snapshot()

    report = _report(snapshot, repository=REPOSITORY)

    assert report["status"] == "passed"
    assert _check(report, "pinned_repository_identity")["status"] == "passed"


def test_live_cli_reports_content_free_blocked_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr(SCRIPT.shutil, "which", lambda _name: "/usr/bin/gh")
    snapshot = _snapshot()
    snapshot["environment"] = None
    snapshot["environment_secrets"] = {"secrets": [], "total_count": 0}
    snapshot["runners"] = {"runners": [], "total_count": 0}
    monkeypatch.setattr(SCRIPT, "collect_snapshot", lambda **_arguments: snapshot)

    assert SCRIPT.main(["--repository", REPOSITORY]) == 3
    output = capsys.readouterr().out
    report = SCRIPT.json.loads(output)
    assert report["status"] == "blocked"
    assert report["public_claim_eligible"] is False
    assert "reviewer" not in output.lower() or "reviewer_count" in output
