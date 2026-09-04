from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/oliver-ci.yml"


def _authority_job() -> str:
    return WORKFLOW.read_text().split("  browser-authority:\n", 1)[1].split("  browser-isolation:\n", 1)[0]


def _environment() -> dict:
    return {
        "name": "browser-hardening",
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "protected_branches": True,
            "custom_branch_policies": False,
        },
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 184999458, "login": "Seabass-up"}}],
            }
        ],
    }


@pytest.mark.parametrize(
    "case",
    [
        "owner",
        "another_user",
        "another_login",
        "team",
        "extra_reviewer",
        "no_reviewer",
        "extra_rule",
        "missing_rule",
        "self_review_prevented",
        "missing_self_review",
        "null_self_review",
        "numeric_self_review",
        "admin_bypass",
        "unprotected_branches",
        "custom_branches",
        "wrong_environment",
        "string_id",
        "malformed_row",
        "duplicate_key",
    ],
)
def test_live_environment_validator(tmp_path: Path, case: str) -> None:
    document = _environment()
    rules = document["protection_rules"]
    rule = rules[0]
    rows = rule["reviewers"]
    if case == "another_user":
        rows[0]["reviewer"]["id"] = 7
    elif case == "another_login":
        rows[0]["reviewer"]["login"] = "another-user"
    elif case == "team":
        rows[0]["type"] = "Team"
    elif case == "extra_reviewer":
        rows.append({"type": "User", "reviewer": {"id": 7, "login": "another-user"}})
    elif case == "no_reviewer":
        rows.clear()
    elif case == "extra_rule":
        rules.append(rule.copy())
    elif case == "missing_rule":
        rules.clear()
    elif case == "self_review_prevented":
        rule["prevent_self_review"] = True
    elif case == "missing_self_review":
        del rule["prevent_self_review"]
    elif case == "null_self_review":
        rule["prevent_self_review"] = None
    elif case == "numeric_self_review":
        rule["prevent_self_review"] = 0
    elif case == "admin_bypass":
        document["can_admins_bypass"] = True
    elif case == "unprotected_branches":
        document["deployment_branch_policy"]["protected_branches"] = False
    elif case == "custom_branches":
        document["deployment_branch_policy"]["custom_branch_policies"] = True
    elif case == "wrong_environment":
        document["name"] = "pypi"
    elif case == "string_id":
        rows[0]["reviewer"]["id"] = "184999458"
    elif case == "malformed_row":
        rows[0] = None
    payload = json.dumps(document)
    if case == "duplicate_key":
        payload = payload.replace('"can_admins_bypass": false', '"can_admins_bypass": true, "can_admins_bypass": false')
    source = textwrap.dedent(_authority_job().split("<<'PY'\n", 1)[1].split("          PY\n", 1)[0])
    snapshot = tmp_path / "environment.json"
    snapshot.write_text(payload)
    result = subprocess.run(
        [sys.executable, "-I", "-", str(snapshot)],
        input=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert (result.returncode == 0) is (case == "owner"), result.stderr
    if case != "owner":
        assert "Boron environment authority verification failed" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="GitHub authority runs on Linux")
@pytest.mark.parametrize(
    ("actor_id", "triggering_actor", "allowed"),
    [
        ("184999458", "Seabass-up", True),
        ("7", "Seabass-up", False),
        ("184999458", "another-user", False),
        ("", "Seabass-up", False),
    ],
)
def test_authority_rejects_other_initiators(actor_id: str, triggering_actor: str, allowed: bool) -> None:
    # Run the real pre-network authority gate without issuing any GitHub requests.
    source = textwrap.dedent(_authority_job().split("        run: |\n", 1)[1].split("          response=", 1)[0])
    result = subprocess.run(
        ["bash", "-c", source],
        env={
            "HENRY_REPOSITORY_ID": "1297752684",
            "HENRY_REF_PROTECTED": "true",
            "HENRY_SOURCE_SHA": "a" * 40,
            "HENRY_WORKFLOW_SHA": "a" * 40,
            "BORON_HARDENING_ENVIRONMENT_READY": "true",
            "HENRY_ACTOR_ID": actor_id,
            "HENRY_TRIGGERING_ACTOR": triggering_actor,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert (result.returncode == 0) is allowed
