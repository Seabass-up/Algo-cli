"""Bounded adapter to the separately installed Jev question-contract companion."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import Any


MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 48_000
MAX_RESPONSE_BYTES = 128_000
TIMEOUT_SECONDS = 20
MODES = frozenset({"lint", "review", "run", "followup"})
RESULT_SCHEMA = "jev.question-contract-result.v1"


class JevKernelError(RuntimeError):
    """A fixed diagnostic code without provider bodies or submitted evidence."""


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _failure(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "advisory_only": True,
        "error": code,
        "answers": {},
        "fallback": "continue_with_existing_workflow",
    }


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise JevKernelError("jev_invalid_response")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> Any:
    raise JevKernelError("jev_invalid_response")


def companion_path(cfg: Any) -> Path:
    value = getattr(cfg, "jev_kernel_cli", "")
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise JevKernelError("jev_companion_not_configured")
    path = Path(value).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise JevKernelError("jev_companion_unavailable")
    return path


def _invoke(cfg: Any, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = _encoded(payload)
    if len(body) > MAX_REQUEST_BYTES:
        raise JevKernelError("jev_request_too_large")
    cli = companion_path(cfg)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONSAFEPATH"] = "1"
    # Drain only a bounded reply; never persist payloads or echo child stderr.
    reply = bytearray()
    errors: list[str] = []
    with subprocess.Popen(
        [str(cli), operation], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=cli.parent, env=env, bufsize=0,
        start_new_session=os.name == "posix",
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def send() -> None:
            try:
                remaining = memoryview(body)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise OSError("write did not advance")
                    remaining = remaining[written:]
                process.stdin.close()
            except OSError:
                errors.append("jev_companion_io_error")

        def receive() -> None:
            try:
                while True:
                    chunk = process.stdout.read(min(8192, MAX_RESPONSE_BYTES + 1 - len(reply)))
                    if not chunk:
                        break
                    reply.extend(chunk)
                    if len(reply) > MAX_RESPONSE_BYTES:
                        errors.append("jev_response_too_large")
                        process.kill()
                        break
            except OSError:
                errors.append("jev_companion_io_error")

        workers = [threading.Thread(target=send, daemon=True), threading.Thread(target=receive, daemon=True)]
        for worker in workers:
            worker.start()
        try:
            process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            errors.append("jev_companion_timeout")
        finally:
            # A companion's children must not retain our pipes after it exits.
            # Unbuffered streams also avoid close waiting on a reader's lock.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for worker in workers:
                worker.join(timeout=1)
        if any(worker.is_alive() for worker in workers):
            raise JevKernelError("jev_companion_io_error")
        if errors:
            raise JevKernelError(errors[0])
        value = json.loads(reply, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        if (
            type(value) is not dict or type(value.get("ok")) is not bool
            or process.returncode not in {0, 1}
            or (process.returncode == 0) != value["ok"]
        ):
            raise JevKernelError("jev_invalid_response")
        return value


def _valid_choice(answer: Any, choices: set[str]) -> bool:
    if type(answer) is not dict or answer.get("type") != "choice":
        return False
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")
    return (
        type(answer.get("choice")) is str and answer["choice"] in choices
        and type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
        and type(probabilities) is dict and set(probabilities) == choices
        and all(type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
        and abs(sum(probabilities.values()) - 1) <= 0.02
    )


def _validate_answers(value: dict[str, Any], contract: dict, mode: str) -> None:
    if not value["ok"] or mode == "lint":
        if value["answers"]:
            raise JevKernelError("jev_invalid_contract_response")
        return
    items = contract.get("items")
    if type(items) is not dict or set(value["answers"]) != set(items):
        raise JevKernelError("jev_answer_ids_mismatch")
    for key, answer in value["answers"].items():
        if type(answer) is not dict:
            raise JevKernelError("jev_invalid_answer")
        if mode == "review":
            axes = {"goal_alignment", "focused_judgment", "answer_shape", "criteria_meaning", "evidence_meaning"}
            if set(answer) != axes or not all(_valid_choice(a, {"satisfied", "problem", "unknown"}) for a in answer.values()):
                raise JevKernelError("jev_invalid_review_answer")
            continue
        status = answer.get("status")
        if status in {"needs_review", "needs_evidence", "no_match"}:
            if "value" not in answer or answer["value"] is not None:
                raise JevKernelError("jev_invalid_answer")
            continue
        if status != "answered" or type(items[key]) is not dict:
            raise JevKernelError("jev_invalid_answer")
        shape = items[key].get("answer_shape")
        result = answer.get("value")
        question = items[key].get("question", {})
        criteria = question.get("criteria") if type(question) is dict else None
        valid = (
            (shape == "yes_no" and type(result) is bool)
            or (shape == "single_choice" and type(result) is str and type(criteria) is dict
                and result in criteria and result not in {"no_match", "insufficient_evidence"})
            or (shape == "ordinal" and type(result) in (int, float) and math.isfinite(result)
                and type(criteria) is list and 0 <= result <= len(criteria) - 1)
        )
        if not valid:
            raise JevKernelError("jev_invalid_answer")


def kernel_status(cfg: Any) -> dict[str, Any]:
    """Inspect companion compatibility without loading credentials or calling Jev."""
    try:
        value = _invoke(cfg, "status", {})
        if value.get("mode") != "advisory_only" or value.get("model") != MODEL or not value["ok"]:
            raise JevKernelError("jev_incompatible_companion")
        # An old companion can report a healthy status without supporting contracts.
        probe = _invoke(cfg, "contract", {"mode": "lint", "contract": {}})
        if probe.get("schema_version") != RESULT_SCHEMA or probe.get("status") != "invalid_contract":
            raise JevKernelError("jev_contract_unavailable")
        return {
            "ok": True, "advisory_only": True, "model": MODEL,
            "enabled": getattr(cfg, "jev_kernel_enabled", False) is True,
            "contract_schema": "jev.question-contract.v1",
            "api_connectivity": "not_probed_by_status",
            "credential_source": "companion_managed", "persistent_payload_storage": False,
        }
    except JevKernelError as exc:
        return _failure(str(exc))
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        return _failure("jev_companion_unavailable")


def question_contract(
    cfg: Any, contract: dict, mode: str = "lint", parent: dict | None = None,
) -> dict[str, Any]:
    """Delegate the existing contract; never interpret answers as action authority."""
    try:
        if type(mode) is not str or mode not in MODES or type(contract) is not dict:
            raise JevKernelError("jev_invalid_arguments")
        if (mode == "followup") != (parent is not None) or (parent is not None and type(parent) is not dict):
            raise JevKernelError("jev_invalid_parent")
        if mode != "lint" and getattr(cfg, "jev_kernel_enabled", False) is not True:
            raise JevKernelError("jev_kernel_disabled")
        value = _invoke(cfg, "contract", {"contract": contract, "mode": mode, "parent": parent})
        if (
            value.get("schema_version") != RESULT_SCHEMA
            or value.get("advisory_only") is not True
            or value.get("mode") != mode
            or value.get("contract_sha256") != hashlib.sha256(_encoded(contract)).hexdigest()
            or type(value.get("answers")) is not dict
            or value.get("freshness") != "not_verified"
        ):
            raise JevKernelError("jev_invalid_contract_response")
        if value["ok"] and value.get("source_revision") != contract.get("source_revision"):
            raise JevKernelError("jev_source_revision_mismatch")
        if mode != "lint" and value["ok"]:
            inference = value.get("inference")
            if type(inference) is not dict or inference.get("model") != MODEL:
                raise JevKernelError("jev_incompatible_model")
        _validate_answers(value, contract, mode)
        return value
    except JevKernelError as exc:
        return _failure(str(exc))
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        return _failure("jev_companion_unavailable")


def jev_kernel_status(cfg: Any = None) -> str:
    """Inspect Jev companion readiness locally; this makes no provider call."""
    return json.dumps(kernel_status(cfg), ensure_ascii=False)


def jev_question_contract(contract: dict, mode: str = "lint", parent: dict | None = None, cfg: Any = None) -> str:
    """Use Jev for bounded advisory classification, review, routing or claim judgments.

    Supply jev.question-contract.v1: goal, decision_use, state, source_revision,
    items keyed by ID. Each item has answer_shape (yes_no/single_choice/ordinal),
    evidence_paths (JSON pointers), missing_evidence (needs_evidence or
    not_mentioned_is_false for presence-only Nouls), and question with type
    (noul/choice/score), instructions and optional criteria. Choices require
    choice_coverage; open Choices require no_match. Missing evidence stays unknown.
    Default lint is local. Review/run/followup send the supplied packet to TypeSafe.
    Never include credentials, bulk private history or entire memory stores.
    Jev does not execute actions, authorize work, or verify real-world completion.
    Retain unresolved statuses and use normal tools to check consequential claims.
    """
    return json.dumps(question_contract(cfg, contract, mode, parent), ensure_ascii=False)
