#!/usr/bin/env python3
"""Check installed Jev local dispatch without credentials, inference or user state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companion", required=True, type=Path)
    parser.add_argument(
        "--allow-source-drift", action="store_true",
        help="Report, rather than hide, installed/source drift during a scoped local repair.",
    )
    args = parser.parse_args()
    if not args.companion.is_absolute() or not args.companion.is_file():
        parser.error("--companion must name the installed absolute executable")

    with tempfile.TemporaryDirectory(prefix="algo-jev-local-smoke-") as directory:
        # Set the test store before importing the installed package. Never load
        # or save the user's config, memory or runtime receipts in this probe.
        os.environ["ALGO_CLI_CONFIG_DIR"] = directory
        os.environ["ALGO_CLI_DISABLE_WINDOWS_HOME_FALLBACK"] = "1"
        import algo_cli
        from algo_cli.arthur_outcomes import OutcomeStatus
        from algo_cli.config import Config
        from algo_cli.james_dispatch import dispatch_action

        installed_root = Path(algo_cli.__file__).resolve().parent
        source_root = Path(__file__).resolve().parents[1] / "algo_cli"
        assert installed_root != source_root, "Run with the installed interpreter and -I"
        cfg = Config(cwd=directory, jev_kernel_cli=str(args.companion), jev_kernel_enabled=False)
        cfg._nathan_approval_mode = "never"
        status = dispatch_action("jev_kernel_status", {}, cfg, render=False)
        assert status.outcome.status is OutcomeStatus.SUCCEEDED, status.result
        status_body = json.loads(status.result)
        assert status_body["ok"] and status_body["api_connectivity"] == "not_probed_by_status"

        contract = {
            "schema_version": "jev.question-contract.v1",
            "goal": "Check whether a synthetic issue mentions login failure.",
            "decision_use": "Advisory triage only; do not execute any action.",
            "state": {"text": "Login is failing."}, "source_revision": "local-smoke-r1",
            "items": {"login": {
                "answer_shape": "yes_no", "evidence_paths": ["/text"],
                "missing_evidence": "not_mentioned_is_false",
                "question": {"type": "noul", "instructions": "Does the text mention login failure?"},
            }},
        }
        lint = dispatch_action("jev_question_contract", {"contract": contract}, cfg, render=False)
        assert lint.outcome.status is OutcomeStatus.SUCCEEDED, lint.result
        lint_body = json.loads(lint.result)
        assert lint_body["ok"] and lint_body["mode"] == "lint" and lint_body["answers"] == {}
        paid = dispatch_action(
            "jev_question_contract", {"contract": contract, "mode": "run"}, cfg, render=False,
        )
        assert paid.outcome.status is OutcomeStatus.DENIED and not paid.outcome.invoked
        ceiling = dispatch_action(
            "jev_kernel_status", {}, cfg, policy_ceiling_code="agent_tool_not_allowed", render=False,
        )
        assert ceiling.outcome.status is OutcomeStatus.DENIED and not ceiling.outcome.invoked
        hashes, parity = {}, {}
        for name in ("marcus_authority.py", "nathan_runtime.py", "samuel_policy_engine.py"):
            installed = (installed_root / name).read_bytes()
            parity[name] = installed == (source_root / name).read_bytes()
            if not args.allow_source_drift:
                assert parity[name], f"affected source mismatch: {name}"
            hashes[name] = hashlib.sha256(installed).hexdigest()
        print(json.dumps({
            "ok": True, "installed_package": str(installed_root),
            "affected_source_parity": all(parity.values()), "source_parity_by_file": parity,
            "affected_sha256": hashes,
            "status": status_body, "lint_status": lint_body["status"],
            "unapproved_paid_run": "denied_without_invocation",
            "caller_ceiling": "denied_without_invocation",
            "provider_inference_calls": 0,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
