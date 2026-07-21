"""Bounded typed programs over the existing Algo CLI action runtime.

This module intentionally does not execute model-authored Python or JavaScript.
It compiles a small JSON plan whose action steps must pass the same policy,
approval, attempt-ledger, and execution-guardrail path as ordinary tool calls.
Pure transforms operate only on JSON-compatible values produced by earlier
steps. Large or protected values use encrypted, run-capability-scoped artifact
storage outside the conversation and every step produces a frozen, hash-linked,
tamper-evident receipt.

Integration code is expected to construct :class:`ProgramAuthorization` from
the tool set already granted to the active chat or Agent Block.  Authorization
is not part of the model-authored plan and therefore cannot be widened by it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import stat
import tempfile
import time
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Mapping,
    Sequence,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from . import execution_guardrails
from .action_registry import get_action_spec
from .alice_artifact_store import (
    ArtifactPolicy,
    ArtifactStoreError,
    EncryptedArtifactRef,
    EncryptedArtifactStore,
    RunCapability,
)
from .config import CONFIG_DIR, Config
from .irene_privacy_views import keyed_action_fingerprint
from .marcus_authority import (
    ConfirmationMode,
    DataClass,
    EffectClass,
    TargetScope,
)
from .nathan_runtime import classify_tool_status, execute_tool_call_for_pipeline
from .nathan_runtime import tool_runtime_args
from .samuel_policy_engine import resolve_action
from .tools import TOOL_MAP, shell_is_dangerous

if TYPE_CHECKING:
    from .james_dispatch import DispatchCancellation


PROGRAM_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
ZERO_RECEIPT_HASH = "0" * 64
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_ACTIONS = frozenset(
    {
        "action_execute",
        "action_program",
        "action_search",
        "available_actions",
        "plugins_load",
        "program_execute",
        "session_command",
        "session_slash",
    }
)
_TRANSFORM_OPS = frozenset(
    {
        "count",
        "filter_eq",
        "get",
        "join",
        "json_parse",
        "json_stringify",
        "select",
        "sort",
        "take",
        "unique",
    }
)
_PROGRAM_STATUSES = frozenset(
    {
        "worked",
        "failed",
        "denied",
        "skipped",
        "timed_out",
        "cancelled",
        "unknown_outcome",
        "limit_exceeded",
    }
)
_SUCCESSFUL_SHELL_RESULT_RE = re.compile(r"\[exit code:\s*0\]", re.IGNORECASE)
_RUNTIME_OWNED_ACTION_ARGS = frozenset({"cfg", "cwd", "safe_mode"})
_PROTECTED_PROGRAM_DATA = frozenset(
    {
        DataClass.USER_PROFILE,
        DataClass.SENSITIVE,
        DataClass.CREDENTIAL,
        DataClass.AUTHENTICATION,
    }
)


class ProgramValidationError(ValueError):
    """Raised before execution when a typed program violates its contract."""


class ProgramStoreError(RuntimeError):
    """Raised when program artifacts or receipts cannot be stored safely."""


@dataclass(frozen=True)
class ProgramLimits:
    """Resource ceilings for one typed program execution.

    ``max_runtime_seconds`` is a cooperative wall-clock budget. The runtime
    propagates the remaining budget into actions that expose a ``timeout``
    argument and checks the budget around every step. Python cannot safely
    preempt an arbitrary synchronous tool on every supported platform, so an
    action without a timeout contract can finish after the budget and will then
    be recorded as ``limit_exceeded``.
    """

    max_steps: int = 12
    max_runtime_seconds: float = 30.0
    max_intermediate_bytes: int = 1_000_000
    artifact_threshold_bytes: int = 8_000
    max_collection_items: int = 10_000
    output_preview_chars: int = 2_000
    max_outputs: int = 4
    max_plan_bytes: int = 64_000

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not isinstance(self.max_runtime_seconds, (int, float))
            or not math.isfinite(float(self.max_runtime_seconds))
            or self.max_runtime_seconds <= 0
        ):
            raise ValueError("max_runtime_seconds must be a positive finite number")
        for name in (
            "max_intermediate_bytes",
            "artifact_threshold_bytes",
            "max_collection_items",
            "output_preview_chars",
            "max_outputs",
            "max_plan_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ProgramAuthorization:
    """Runtime-owned capability ceiling for nested action steps."""

    allowed_actions: frozenset[str]
    force_approval_actions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        allowed = frozenset(str(name).strip() for name in self.allowed_actions if str(name).strip())
        forced = frozenset(str(name).strip() for name in self.force_approval_actions if str(name).strip())
        if forced - allowed:
            raise ValueError("force_approval_actions must be a subset of allowed_actions")
        object.__setattr__(self, "allowed_actions", allowed)
        object.__setattr__(self, "force_approval_actions", forced)

    @classmethod
    def from_tools(
        cls,
        tools: Sequence[Callable[..., Any]],
        *,
        force_approval_actions: Sequence[str] = (),
    ) -> "ProgramAuthorization":
        names = frozenset(str(getattr(tool, "__name__", "")).strip() for tool in tools)
        return cls(
            frozenset(name for name in names if name),
            frozenset(str(name).strip() for name in force_approval_actions if str(name).strip()),
        )


def authorization_for_actions(
    action_names: Sequence[str],
    *,
    force_approval_actions: Sequence[str] = (),
) -> ProgramAuthorization:
    """Build a runtime-owned ceiling while removing non-composable meta actions."""

    allowed = frozenset(
        str(name).strip() for name in action_names if str(name).strip() and str(name).strip() not in _FORBIDDEN_ACTIONS
    )
    forced = frozenset(str(name).strip() for name in force_approval_actions if str(name).strip())
    return ProgramAuthorization(allowed, forced & allowed)


@dataclass(frozen=True)
class StepReference:
    step_id: str
    path: tuple[str | int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"$ref": self.step_id}
        if self.path:
            value["path"] = list(self.path)
        return value


@dataclass(frozen=True)
class ProgramTaint:
    """Structural trust/data classification propagated through the closed DSL."""

    untrusted: bool
    model_controlled: bool
    data_classes: tuple[DataClass, ...]
    source_steps: tuple[str, ...]

    @property
    def protected(self) -> bool:
        return bool(set(self.data_classes) & _PROTECTED_PROGRAM_DATA)

    def to_dict(self) -> dict[str, Any]:
        return {
            "untrusted": self.untrusted,
            "model_controlled": self.model_controlled,
            "data_classes": [item.value for item in self.data_classes],
            "source_count": len(self.source_steps),
            "protected": self.protected,
        }


@dataclass(frozen=True)
class ProgramActionPreflight:
    """Frozen exact effect resolution produced before program execution."""

    step_id: str
    action: str
    action_digest: str
    target: str
    target_scope: TargetScope
    effect_class: EffectClass
    confirmation_mode: ConfirmationMode
    capability_mask: int
    data_classes: tuple[DataClass, ...]

    @property
    def mutates_state(self) -> bool:
        return self.effect_class is not EffectClass.OBSERVE

    def binding_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "action_digest": self.action_digest,
            "target_scope": self.target_scope.value,
            "effect_class": self.effect_class.value,
            "confirmation_mode": self.confirmation_mode.value,
            "capability_mask": self.capability_mask,
            "data_classes": [item.value for item in self.data_classes],
        }


@dataclass(frozen=True)
class ActionProgramStep:
    step_id: str
    action: str
    args_json: str
    output_taint: ProgramTaint
    kind: str = "action"

    def args(self) -> dict[str, Any]:
        loaded = json.loads(self.args_json)
        if not isinstance(loaded, dict):  # Defensive: compiler always stores an object.
            raise ProgramValidationError(f"action step {self.step_id} args are not an object")
        return loaded


@dataclass(frozen=True)
class TransformProgramStep:
    step_id: str
    op: str
    input_json: str
    args_json: str
    output_taint: ProgramTaint
    kind: str = "transform"

    def input_value(self) -> Any:
        return json.loads(self.input_json)

    def args(self) -> dict[str, Any]:
        loaded = json.loads(self.args_json)
        if not isinstance(loaded, dict):
            raise ProgramValidationError(f"transform step {self.step_id} args are not an object")
        return loaded


ProgramStep = ActionProgramStep | TransformProgramStep


@dataclass(frozen=True)
class CompiledProgram:
    version: int
    plan_hash: str
    steps: tuple[ProgramStep, ...]
    outputs: tuple[StepReference, ...]
    action_preflights: tuple[ProgramActionPreflight, ...]
    preflight_cwd: str
    safe_mode: bool


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    digest: str
    byte_count: int
    media_type: str
    run_id: str = field(default="", repr=False)
    artifact_id: str = field(default="", repr=False)
    content_id: str = field(default="", repr=False)
    expires_at: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "content_id": self.content_id,
            "bytes": self.byte_count,
            "media_type": self.media_type,
        }


@dataclass(frozen=True)
class ProgramReceipt:
    schema_version: int
    run_id: str
    plan_hash: str
    sequence: int
    step_id: str
    kind: str
    operation: str
    mutates_state: bool
    verification_kind: str
    status: str
    input_hash: str
    result_hash: str
    result_bytes: int
    artifact_uri: str
    duration_ms: float
    previous_hash: str
    receipt_hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "sequence": self.sequence,
            "step_id": self.step_id,
            "kind": self.kind,
            "operation": self.operation,
            "mutates_state": self.mutates_state,
            "verification_kind": self.verification_kind,
            "status": self.status,
            "input_hash": self.input_hash,
            "result_hash": self.result_hash,
            "result_bytes": self.result_bytes,
            "artifact_uri": self.artifact_uri,
            "duration_ms": self.duration_ms,
            "previous_hash": self.previous_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "receipt_hash": self.receipt_hash}


@dataclass(frozen=True)
class ProgramOutput:
    reference: StepReference
    media_type: str
    byte_count: int
    sha256: str
    preview: str
    taint: ProgramTaint
    artifact: ArtifactRef | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reference": self.reference.to_dict(),
            "media_type": self.media_type,
            "bytes": self.byte_count,
            "preview": self.preview,
            "taint": self.taint.to_dict(),
        }
        if not self.taint.protected:
            payload["sha256"] = self.sha256
        if self.artifact is not None:
            payload["artifact"] = self.artifact.to_dict()
        return payload


@dataclass(frozen=True)
class ProgramResult:
    status: str
    run_id: str
    plan_hash: str
    outputs: tuple[ProgramOutput, ...]
    receipts: tuple[ProgramReceipt, ...]
    receipt_chain_hash: str
    receipt_uri: str
    error: str = ""
    requires_reconciliation: bool = False

    @property
    def worked(self) -> bool:
        return self.status == "worked"

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "outputs": [output.to_dict() for output in self.outputs],
            "receipt_chain_hash": self.receipt_chain_hash,
            "receipt_uri": self.receipt_uri,
            "error": self.error,
            "requires_reconciliation": self.requires_reconciliation,
        }
        if compact:
            payload["receipt_count"] = len(self.receipts)
            payload["mutation_receipts"] = [
                {
                    "step_id": receipt.step_id,
                    "operation": receipt.operation,
                    "status": receipt.status,
                    "receipt_hash": receipt.receipt_hash,
                    "artifact_uri": receipt.artifact_uri,
                }
                for receipt in self.receipts
                if receipt.mutates_state
            ]
            payload["verification_receipts"] = [
                {
                    "step_id": receipt.step_id,
                    "operation": receipt.operation,
                    "verification_kind": receipt.verification_kind,
                    "status": receipt.status,
                    "receipt_hash": receipt.receipt_hash,
                }
                for receipt in self.receipts
                if receipt.verification_kind
            ]
        else:
            payload["receipts"] = [receipt.to_dict() for receipt in self.receipts]
        return payload


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProgramValidationError("program values must be finite JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _is_reference(value: Any) -> bool:
    return isinstance(value, dict) and "$ref" in value


def _parse_reference(value: Any, *, available: frozenset[str], location: str) -> StepReference:
    if isinstance(value, str):
        value = {"$ref": value}
    if not isinstance(value, dict) or "$ref" not in value:
        raise ProgramValidationError(f"{location} must be a step reference")
    if set(value) - {"$ref", "path"}:
        raise ProgramValidationError(f"{location} reference has unsupported fields")
    step_id = value.get("$ref")
    if not isinstance(step_id, str) or not _STEP_ID_RE.fullmatch(step_id):
        raise ProgramValidationError(f"{location} has an invalid $ref step id")
    if step_id not in available:
        raise ProgramValidationError(f"{location} must reference an earlier step: {step_id}")
    raw_path = value.get("path", [])
    if not isinstance(raw_path, list):
        raise ProgramValidationError(f"{location} reference path must be a list")
    path: list[str | int] = []
    for component in raw_path:
        if isinstance(component, bool) or not isinstance(component, (str, int)):
            raise ProgramValidationError(f"{location} reference path components must be strings or integers")
        if isinstance(component, int) and component < 0:
            raise ProgramValidationError(f"{location} reference indexes cannot be negative")
        path.append(component)
    return StepReference(step_id, tuple(path))


def _validate_json_and_refs(value: Any, *, available: frozenset[str], location: str, depth: int = 0) -> None:
    if depth > 64:
        raise ProgramValidationError(f"{location} nesting exceeds 64 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProgramValidationError(f"{location} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_and_refs(item, available=available, location=f"{location}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if "$ref" in value:
            _parse_reference(value, available=available, location=location)
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProgramValidationError(f"{location} object keys must be strings")
            _validate_json_and_refs(item, available=available, location=f"{location}.{key}", depth=depth + 1)
        return
    raise ProgramValidationError(f"{location} contains a non-JSON value")


def _check_object_fields(value: Mapping[str, Any], allowed: frozenset[str], *, location: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ProgramValidationError(f"{location} has unsupported fields: {', '.join(sorted(unknown))}")


def _normalized_compiled_plan(
    steps: Sequence[ProgramStep],
    outputs: Sequence[StepReference],
) -> dict[str, Any]:
    normalized_steps: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, ActionProgramStep):
            normalized_steps.append(
                {
                    "id": step.step_id,
                    "kind": "action",
                    "action": step.action,
                    "args": step.args(),
                }
            )
        else:
            normalized_steps.append(
                {
                    "id": step.step_id,
                    "kind": "transform",
                    "op": step.op,
                    "input": step.input_value(),
                    "args": step.args(),
                }
            )
    return {
        "version": PROGRAM_SCHEMA_VERSION,
        "steps": normalized_steps,
        "outputs": [reference.to_dict() for reference in outputs],
    }


def _reference_step_ids(value: Any) -> frozenset[str]:
    found: set[str] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if _is_reference(current):
            step_id = current.get("$ref")
            if isinstance(step_id, str):
                found.add(step_id)
            continue
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
    return frozenset(found)


def _literal_taint() -> ProgramTaint:
    return ProgramTaint(
        untrusted=True,
        model_controlled=True,
        data_classes=(DataClass.PUBLIC,),
        source_steps=("model_plan",),
    )


def _merge_taints(taints: Sequence[ProgramTaint]) -> ProgramTaint:
    if not taints:
        return _literal_taint()
    data_classes = tuple(
        sorted(
            {item for taint in taints for item in taint.data_classes},
            key=lambda item: item.value,
        )
    )
    sources = tuple(sorted({source for taint in taints for source in taint.source_steps}))
    return ProgramTaint(
        untrusted=any(taint.untrusted for taint in taints),
        model_controlled=True,
        data_classes=data_classes or (DataClass.PUBLIC,),
        source_steps=sources or ("model_plan",),
    )


def _validate_annotation(value: Any, annotation: Any, *, location: str) -> None:
    if annotation is Any or annotation is inspect.Parameter.empty:
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        _validate_annotation(value, args[0], location=location)
        return
    if origin in {Union, types.UnionType}:
        for option in args:
            try:
                _validate_annotation(value, option, location=location)
            except ProgramValidationError:
                continue
            return
        raise ProgramValidationError(f"{location} has the wrong JSON type")
    if annotation is type(None):
        if value is not None:
            raise ProgramValidationError(f"{location} must be null")
        return
    if annotation is str:
        if not isinstance(value, str):
            raise ProgramValidationError(f"{location} must be a string")
        return
    if annotation is bool:
        if not isinstance(value, bool):
            raise ProgramValidationError(f"{location} must be a boolean")
        return
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProgramValidationError(f"{location} must be an integer")
        return
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ProgramValidationError(f"{location} must be a finite number")
        return
    if annotation is dict or origin is dict:
        if not isinstance(value, dict):
            raise ProgramValidationError(f"{location} must be an object")
        if args:
            key_type, value_type = args
            for key, item in value.items():
                _validate_annotation(key, key_type, location=f"{location} key")
                _validate_annotation(item, value_type, location=f"{location}.{key}")
        return
    if annotation is list or origin is list:
        if not isinstance(value, list):
            raise ProgramValidationError(f"{location} must be a list")
        if args:
            for index, item in enumerate(value):
                _validate_annotation(item, args[0], location=f"{location}[{index}]")
        return
    raise ProgramValidationError(f"{location} uses an unsupported runtime annotation")


def _validate_action_args(action: str, args: Mapping[str, Any], *, location: str) -> None:
    runtime_owned = set(args) & _RUNTIME_OWNED_ACTION_ARGS
    if runtime_owned:
        raise ProgramValidationError(
            f"{location} attempts to set runtime-owned fields: {', '.join(sorted(runtime_owned))}"
        )
    if _reference_step_ids(args):
        raise ProgramValidationError(
            f"{location} cannot contain step references; action inputs must be static "
            "until typed output schemas and declassification are available"
        )
    try:
        fn = TOOL_MAP[action]
        signature = inspect.signature(fn)
        if any(
            parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in signature.parameters.values()
        ):
            raise ProgramValidationError(f"{location} action schema is open-ended and cannot be compiled")
        bound = signature.bind(**dict(args))
        hints = get_type_hints(fn, include_extras=True)
    except KeyError as exc:
        raise ProgramValidationError(f"{location} names an unknown action") from exc
    except TypeError as exc:
        raise ProgramValidationError(f"{location} does not match the action schema: {exc}") from exc
    except (NameError, ValueError) as exc:
        raise ProgramValidationError(f"{location} action schema is unavailable") from exc
    for name, value in bound.arguments.items():
        annotation = hints.get(name, signature.parameters[name].annotation)
        _validate_annotation(value, annotation, location=f"{location}.{name}")
    if "timeout" in args:
        timeout = args["timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ProgramValidationError(f"{location}.timeout must be positive and finite")


def _validate_transform_contract(
    op: str,
    transform_input: Any,
    args: Mapping[str, Any],
    *,
    limits: ProgramLimits,
    location: str,
) -> None:
    allowed_fields = {
        "json_parse": frozenset(),
        "json_stringify": frozenset(),
        "get": frozenset({"path"}),
        "count": frozenset(),
        "filter_eq": frozenset({"path", "equals"}),
        "sort": frozenset({"path", "descending"}),
        "take": frozenset({"count"}),
        "unique": frozenset({"path"}),
        "select": frozenset({"fields"}),
        "join": frozenset({"separator"}),
    }[op]
    _check_object_fields(args, allowed_fields, location=f"{location}.args")
    if _reference_step_ids(args):
        raise ProgramValidationError(f"{location}.args must be static")
    path = args.get("path", [])
    if op == "get" and "path" not in args:
        raise ProgramValidationError(f"{location}.args.path is required")
    if "path" in allowed_fields:
        if not isinstance(path, list):
            raise ProgramValidationError(f"{location}.args.path must be a list")
        for component in path:
            if (
                isinstance(component, bool)
                or not isinstance(component, (str, int))
                or isinstance(component, int)
                and component < 0
            ):
                raise ProgramValidationError(
                    f"{location}.args.path components must be strings or non-negative integers"
                )
    if op == "filter_eq" and "equals" not in args:
        raise ProgramValidationError(f"{location}.args.equals is required")
    if op == "sort" and not isinstance(args.get("descending", False), bool):
        raise ProgramValidationError(f"{location}.args.descending must be a boolean")
    if op == "take":
        count = args.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProgramValidationError(f"{location}.args.count must be a non-negative integer")
    if op == "select":
        fields = args.get("fields")
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > 64
            or any(not isinstance(item, str) or not item for item in fields)
        ):
            raise ProgramValidationError(f"{location}.args.fields must contain 1 to 64 non-empty strings")
    if op == "join":
        separator = args.get("separator", "\n")
        if not isinstance(separator, str) or len(separator) > 32:
            raise ProgramValidationError(f"{location}.args.separator must be a string of at most 32 characters")
    if not _reference_step_ids(transform_input):
        try:
            _deterministic_transform(op, transform_input, args, limits)
        except ProgramValidationError as exc:
            raise ProgramValidationError(f"{location} literal transform is invalid: {exc}") from exc


def _canonical_program_cwd(cwd: str) -> str:
    try:
        path = Path(cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProgramValidationError("program workspace cannot be resolved") from exc
    if not path.is_dir():
        raise ProgramValidationError("program workspace must be a directory")
    return str(path)


def _build_action_preflight(
    step_id: str,
    action: str,
    args: Mapping[str, Any],
    *,
    cwd: str,
    safe_mode: bool,
    location: str,
) -> ProgramActionPreflight:
    _validate_action_args(action, args, location=f"{location}.args")
    runtime_args = tool_runtime_args(
        action,
        dict(args),
        Config(cwd=cwd, safe_mode=safe_mode),
    )
    resolved = resolve_action(action, runtime_args, cwd=cwd)
    try:
        spec = get_action_spec(action)
    except KeyError as exc:
        raise ProgramValidationError(f"{location} has no registered ActionSpec") from exc
    if not spec.curated or resolved.effect_class is EffectClass.UNCLASSIFIED:
        raise ProgramValidationError(f"{location} has no curated runtime authority")
    if resolved.target.endswith(":unresolved"):
        raise ProgramValidationError(f"{location} target cannot be resolved before execution")
    if resolved.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED:
        raise ProgramValidationError(f"{location} requires a trusted handoff and cannot run in a program")
    if action == "run_shell" and safe_mode and shell_is_dangerous(str(args.get("command") or "")):
        raise ProgramValidationError(f"{location} is blocked by safe shell policy")
    return ProgramActionPreflight(
        step_id=step_id,
        action=action,
        action_digest=resolved.action_digest,
        target=resolved.target,
        target_scope=resolved.target_scope,
        effect_class=resolved.effect_class,
        confirmation_mode=resolved.confirmation_mode,
        capability_mask=resolved.capability_mask,
        data_classes=resolved.data_classes,
    )


def compile_program(
    plan: Mapping[str, Any],
    *,
    authorization: ProgramAuthorization,
    limits: ProgramLimits = ProgramLimits(),
    cwd: str = ".",
    safe_mode: bool = True,
) -> CompiledProgram:
    """Validate and freeze a model-authored typed program before any action runs."""

    if not isinstance(safe_mode, bool):
        raise ProgramValidationError("program safe_mode must be a runtime-owned boolean")
    canonical_cwd = _canonical_program_cwd(cwd)
    if not isinstance(plan, Mapping):
        raise ProgramValidationError("program must be a JSON object")
    plan_dict = dict(plan)
    plan_bytes = len(_canonical_json(plan_dict).encode("utf-8"))
    if plan_bytes > limits.max_plan_bytes:
        raise ProgramValidationError(f"program is {plan_bytes} bytes; maximum is {limits.max_plan_bytes}")
    _check_object_fields(plan_dict, frozenset({"version", "steps", "outputs"}), location="program")
    if plan_dict.get("version") != PROGRAM_SCHEMA_VERSION:
        raise ProgramValidationError(f"program version must be {PROGRAM_SCHEMA_VERSION}")
    raw_steps = plan_dict.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ProgramValidationError("program steps must be a non-empty list")
    if len(raw_steps) > limits.max_steps:
        raise ProgramValidationError(f"program has {len(raw_steps)} steps; maximum is {limits.max_steps}")

    available: set[str] = set()
    compiled_steps: list[ProgramStep] = []
    taint_by_step: dict[str, ProgramTaint] = {}
    action_preflights: list[ProgramActionPreflight] = []
    for index, raw_step in enumerate(raw_steps):
        location = f"steps[{index}]"
        if not isinstance(raw_step, Mapping):
            raise ProgramValidationError(f"{location} must be an object")
        step = dict(raw_step)
        step_id = step.get("id")
        if not isinstance(step_id, str) or not _STEP_ID_RE.fullmatch(step_id):
            raise ProgramValidationError(f"{location}.id must match {_STEP_ID_RE.pattern}")
        if step_id in available:
            raise ProgramValidationError(f"duplicate step id: {step_id}")
        kind = step.get("kind")
        earlier = frozenset(available)
        if kind == "action":
            _check_object_fields(step, frozenset({"id", "kind", "action", "args"}), location=location)
            action = step.get("action")
            if not isinstance(action, str) or not action.strip():
                raise ProgramValidationError(f"{location}.action must be a non-empty string")
            action = action.strip()
            if action in _FORBIDDEN_ACTIONS:
                raise ProgramValidationError(f"{location} cannot call recursive or session meta action: {action}")
            if action not in TOOL_MAP:
                raise ProgramValidationError(f"{location} names an unknown runtime action: {action}")
            if action not in authorization.allowed_actions:
                raise ProgramValidationError(f"{location} action is outside the runtime capability ceiling: {action}")
            args = step.get("args", {})
            if not isinstance(args, dict):
                raise ProgramValidationError(f"{location}.args must be an object")
            _validate_json_and_refs(args, available=earlier, location=f"{location}.args")
            preflight = _build_action_preflight(
                step_id,
                action,
                args,
                cwd=canonical_cwd,
                safe_mode=safe_mode,
                location=location,
            )
            output_taint = ProgramTaint(
                untrusted=True,
                model_controlled=False,
                data_classes=tuple(sorted(set(preflight.data_classes), key=lambda item: item.value)),
                source_steps=(step_id,),
            )
            compiled_steps.append(
                ActionProgramStep(
                    step_id,
                    action,
                    _canonical_json(args),
                    output_taint,
                )
            )
            taint_by_step[step_id] = output_taint
            action_preflights.append(preflight)
        elif kind == "transform":
            _check_object_fields(step, frozenset({"id", "kind", "op", "input", "args"}), location=location)
            op = step.get("op")
            if not isinstance(op, str) or op not in _TRANSFORM_OPS:
                allowed_ops = ", ".join(sorted(_TRANSFORM_OPS))
                raise ProgramValidationError(f"{location}.op must be one of: {allowed_ops}")
            if "input" not in step:
                raise ProgramValidationError(f"{location}.input is required")
            transform_input = step["input"]
            args = step.get("args", {})
            if not isinstance(args, dict):
                raise ProgramValidationError(f"{location}.args must be an object")
            _validate_json_and_refs(transform_input, available=earlier, location=f"{location}.input")
            _validate_json_and_refs(args, available=earlier, location=f"{location}.args")
            _validate_transform_contract(
                op,
                transform_input,
                args,
                limits=limits,
                location=location,
            )
            referenced_steps = _reference_step_ids(transform_input) | _reference_step_ids(args)
            output_taint = _merge_taints([taint_by_step[referenced] for referenced in sorted(referenced_steps)])
            compiled_steps.append(
                TransformProgramStep(
                    step_id,
                    op,
                    _canonical_json(transform_input),
                    _canonical_json(args),
                    output_taint,
                )
            )
            taint_by_step[step_id] = output_taint
        else:
            raise ProgramValidationError(f"{location}.kind must be 'action' or 'transform'")
        available.add(step_id)

    raw_outputs = plan_dict.get("outputs")
    if raw_outputs is None:
        raw_outputs = [compiled_steps[-1].step_id]
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ProgramValidationError("program outputs must be a non-empty list")
    if len(raw_outputs) > limits.max_outputs:
        raise ProgramValidationError(f"program has {len(raw_outputs)} outputs; maximum is {limits.max_outputs}")
    all_steps = frozenset(available)
    outputs = tuple(
        _parse_reference(value, available=all_steps, location=f"outputs[{index}]")
        for index, value in enumerate(raw_outputs)
    )
    mutating = [preflight for preflight in action_preflights if preflight.mutates_state]
    if len(mutating) > 1:
        raise ProgramValidationError("programs may contain at most one state-changing, code, or external action")
    if mutating:
        mutation = mutating[0]
        if compiled_steps[-1].step_id != mutation.step_id:
            raise ProgramValidationError("a state-changing, code, or external action must be the final program step")
        if len(outputs) != 1 or outputs[0].step_id != mutation.step_id or outputs[0].path:
            raise ProgramValidationError("a mutating program output must directly reference its final action")
    binding = {
        "program": _normalized_compiled_plan(compiled_steps, outputs),
        "workspace_identity": _sha256_bytes(canonical_cwd.encode("utf-8")),
        "safe_mode": safe_mode,
        "actions": [preflight.binding_dict() for preflight in action_preflights],
    }
    plan_hash = keyed_action_fingerprint("action_program_plan", binding)
    return CompiledProgram(
        PROGRAM_SCHEMA_VERSION,
        plan_hash,
        tuple(compiled_steps),
        outputs,
        tuple(action_preflights),
        canonical_cwd,
        safe_mode,
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ProgramStoreError("program store directories must be real directories")
    if os.name == "posix":
        os.chmod(path, 0o700)


def _fsync_program_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _purge_legacy_plaintext_artifacts(root: Path) -> int:
    """Remove only the exact pre-Alice content-addressed directory shape."""

    legacy = root / "artifacts"
    try:
        legacy_info = legacy.lstat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ProgramStoreError("legacy artifact directory cannot be inspected") from exc
    if legacy.is_symlink() or not stat.S_ISDIR(legacy_info.st_mode):
        raise ProgramStoreError("legacy artifact path is not a real directory")
    buckets: list[Path] = []
    blobs: list[Path] = []
    for bucket in list(legacy.iterdir()):
        try:
            info = bucket.lstat()
        except OSError as exc:
            raise ProgramStoreError("legacy artifact bucket cannot be inspected") from exc
        if bucket.is_symlink() or not stat.S_ISDIR(info.st_mode) or not re.fullmatch(r"[0-9a-f]{2}", bucket.name):
            raise ProgramStoreError("legacy artifact directory has an unknown shape")
        buckets.append(bucket)
        for blob in list(bucket.iterdir()):
            try:
                blob_info = blob.lstat()
            except OSError as exc:
                raise ProgramStoreError("legacy artifact file cannot be inspected") from exc
            if (
                blob.is_symlink()
                or not stat.S_ISREG(blob_info.st_mode)
                or not re.fullmatch(r"[0-9a-f]{64}\.blob", blob.name)
            ):
                raise ProgramStoreError("legacy artifact directory has an unknown shape")
            blobs.append(blob)
    # Validation completes before the first destructive operation.
    for blob in blobs:
        blob.unlink()
    for bucket in buckets:
        _fsync_program_directory(bucket)
        bucket.rmdir()
    _fsync_program_directory(legacy)
    legacy.rmdir()
    _fsync_program_directory(root)
    return len(blobs)


class ProgramArtifactStore:
    """Encrypted run-scoped artifacts plus atomically published receipt ledgers.

    Artifact capabilities remain process memory only.  ``begin_run`` binds the
    next artifact write to the program's runtime-owned run ID; the OS-backed
    Alice key is loaded lazily so programs with only compact outputs do not
    touch the credential store.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        key_store: Any | None = None,
        artifact_policy: ArtifactPolicy = ArtifactPolicy(),
        encrypted_store: EncryptedArtifactStore | None = None,
    ) -> None:
        configured = Path(root) if root is not None else CONFIG_DIR / "private" / "program_runtime"
        self.root = Path(os.path.abspath(str(configured.expanduser())))
        _ensure_private_directory(self.root)
        self.legacy_plaintext_artifacts_removed = _purge_legacy_plaintext_artifacts(self.root)
        _ensure_private_directory(self.root / "receipts")
        self._encrypted = encrypted_store or EncryptedArtifactStore(
            self.root / "alice-artifacts-v1",
            policy=artifact_policy,
            key_store=key_store,
        )
        self._active_run_id = ""
        self._capabilities: dict[str, RunCapability] = {}

    def begin_run(self, run_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ProgramStoreError("run id is invalid")
        self._active_run_id = run_id

    def _active_capability(self) -> RunCapability:
        run_id = self._active_run_id or uuid.uuid4().hex
        self._active_run_id = run_id
        capability = self._capabilities.get(run_id)
        if capability is None:
            try:
                capability = self._encrypted.create_run(run_id=run_id)
            except ArtifactStoreError as exc:
                raise ProgramStoreError(f"encrypted artifact run creation failed: {type(exc).__name__}") from exc
            self._capabilities[run_id] = capability
        return capability

    @staticmethod
    def _atomic_publish_new(path: Path, content: bytes) -> None:
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ProgramStoreError("program store target is not a regular file")
            if path.read_bytes() != content:
                raise ProgramStoreError("content-addressed store collision")
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                    raise ProgramStoreError("content-addressed store collision")
            if os.name == "posix" and path.exists():
                os.chmod(path, 0o600)
            _fsync_program_directory(path.parent)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except OSError:
                pass

    def put(self, content: bytes, *, media_type: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        digest = _sha256_bytes(content)
        capability = self._active_capability()
        try:
            ref = self._encrypted.put(
                capability,
                content,
                media_type=media_type,
            )
        except ArtifactStoreError as exc:
            raise ProgramStoreError(f"encrypted artifact write failed: {type(exc).__name__}") from exc
        return ArtifactRef(
            ref.uri,
            digest,
            len(content),
            media_type,
            run_id=ref.run_id,
            artifact_id=ref.artifact_id,
            content_id=ref.content_id,
            expires_at=ref.expires_at,
        )

    def read(self, ref: ArtifactRef) -> bytes:
        if (
            not _HASH_RE.fullmatch(ref.digest)
            or not ref.run_id
            or not ref.artifact_id
            or not ref.content_id
            or ref.expires_at <= 0
        ):
            raise ProgramStoreError("encrypted artifact reference is invalid")
        capability = self._capabilities.get(ref.run_id)
        if capability is None:
            raise ProgramStoreError("encrypted artifact capability is unavailable")
        encrypted_ref = EncryptedArtifactRef(
            ref.uri,
            ref.run_id,
            ref.artifact_id,
            ref.content_id,
            ref.byte_count,
            ref.media_type,
            ref.expires_at,
        )
        try:
            content = self._encrypted.read(capability, encrypted_ref)
        except ArtifactStoreError as exc:
            raise ProgramStoreError(f"encrypted artifact read failed: {type(exc).__name__}") from exc
        if len(content) != ref.byte_count or _sha256_bytes(content) != ref.digest:
            raise ProgramStoreError("artifact integrity check failed")
        return content

    def revoke_run(self, run_id: str) -> None:
        capability = self._capabilities.pop(run_id, None)
        if capability is None:
            raise ProgramStoreError("encrypted artifact capability is unavailable")
        try:
            self._encrypted.revoke_run(capability)
        except ArtifactStoreError as exc:
            raise ProgramStoreError(f"encrypted artifact revocation failed: {type(exc).__name__}") from exc
        if self._active_run_id == run_id:
            self._active_run_id = ""

    def cleanup_artifacts(self) -> dict[str, Any]:
        try:
            return self._encrypted.cleanup().to_dict()
        except ArtifactStoreError as exc:
            raise ProgramStoreError(f"encrypted artifact cleanup failed: {type(exc).__name__}") from exc

    def write_receipts(self, run_id: str, receipts: Sequence[ProgramReceipt]) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ProgramStoreError("run id is invalid")
        content = b"".join((_canonical_json(receipt.to_dict()) + "\n").encode("utf-8") for receipt in receipts)
        ledger_hash = _sha256_bytes(content)
        path = self.root / "receipts" / f"{run_id}-{ledger_hash}.jsonl"
        self._atomic_publish_new(path, content)
        return f"receipt://sha256/{ledger_hash}"


def _resolve_reference(reference: StepReference, values: Mapping[str, Any]) -> Any:
    try:
        current = values[reference.step_id]
    except KeyError as exc:
        raise ProgramValidationError(f"step result is unavailable: {reference.step_id}") from exc
    for component in reference.path:
        if isinstance(component, int):
            if not isinstance(current, list) or component >= len(current):
                raise ProgramValidationError(f"reference {reference.step_id} path index {component} is unavailable")
            current = current[component]
        else:
            if not isinstance(current, dict) or component not in current:
                raise ProgramValidationError(f"reference {reference.step_id} path key {component!r} is unavailable")
            current = current[component]
    return current


def _resolve_refs(value: Any, values: Mapping[str, Any]) -> Any:
    if _is_reference(value):
        return _resolve_reference(
            _parse_reference(value, available=frozenset(values), location="runtime reference"),
            values,
        )
    if isinstance(value, list):
        return [_resolve_refs(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_refs(item, values) for key, item in value.items()}
    return value


def _path_arg(args: Mapping[str, Any], *, required: bool = False) -> tuple[str | int, ...]:
    raw = args.get("path", [])
    if required and "path" not in args:
        raise ProgramValidationError("transform requires args.path")
    if not isinstance(raw, list):
        raise ProgramValidationError("transform args.path must be a list")
    path: list[str | int] = []
    for component in raw:
        if isinstance(component, bool) or not isinstance(component, (str, int)):
            raise ProgramValidationError("transform path components must be strings or integers")
        if isinstance(component, int) and component < 0:
            raise ProgramValidationError("transform path indexes cannot be negative")
        path.append(component)
    return tuple(path)


def _get_path(value: Any, path: Sequence[str | int]) -> Any:
    current = value
    for component in path:
        if isinstance(component, int):
            if not isinstance(current, list) or component >= len(current):
                raise ProgramValidationError(f"transform path index {component} is unavailable")
            current = current[component]
        else:
            if not isinstance(current, dict) or component not in current:
                raise ProgramValidationError(f"transform path key {component!r} is unavailable")
            current = current[component]
    return current


def _expect_list(value: Any, op: str, limits: ProgramLimits) -> list[Any]:
    if not isinstance(value, list):
        raise ProgramValidationError(f"{op} input must be a list")
    if len(value) > limits.max_collection_items:
        raise ProgramValidationError(f"{op} input has {len(value)} items; maximum is {limits.max_collection_items}")
    return value


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return 0, ""
    if isinstance(value, bool):
        return 1, int(value)
    if isinstance(value, (int, float)):
        return 2, value
    if isinstance(value, str):
        return 3, value
    if isinstance(value, list):
        return 4, _canonical_json(value)
    return 5, _canonical_json(value)


def _deterministic_transform(op: str, value: Any, args: Mapping[str, Any], limits: ProgramLimits) -> Any:
    if op == "json_parse":
        if not isinstance(value, str):
            raise ProgramValidationError("json_parse input must be a string")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProgramValidationError(f"json_parse input is invalid JSON: {exc.msg}") from exc
        _canonical_json(parsed)
        return parsed
    if op == "json_stringify":
        return _canonical_json(value)
    if op == "get":
        return _get_path(value, _path_arg(args, required=True))
    if op == "count":
        if not isinstance(value, (list, dict, str)):
            raise ProgramValidationError("count input must be a list, object, or string")
        return len(value)

    items = _expect_list(value, op, limits)
    if op == "filter_eq":
        path = _path_arg(args)
        if "equals" not in args:
            raise ProgramValidationError("filter_eq requires args.equals")
        expected = args["equals"]
        selected: list[Any] = []
        for item in items:
            try:
                candidate = _get_path(item, path)
            except ProgramValidationError:
                continue
            if candidate == expected and type(candidate) is type(expected):
                selected.append(item)
        return selected
    if op == "sort":
        path = _path_arg(args)
        descending = args.get("descending", False)
        if not isinstance(descending, bool):
            raise ProgramValidationError("sort args.descending must be a boolean")
        return sorted(items, key=lambda item: _sort_key(_get_path(item, path)), reverse=descending)
    if op == "take":
        count = args.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProgramValidationError("take args.count must be a non-negative integer")
        return items[: min(count, limits.max_collection_items)]
    if op == "unique":
        path = _path_arg(args)
        seen: set[str] = set()
        unique: list[Any] = []
        for item in items:
            key = _canonical_json(_get_path(item, path))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique
    if op == "select":
        fields = args.get("fields")
        if not isinstance(fields, list) or not fields or any(not isinstance(field, str) for field in fields):
            raise ProgramValidationError("select args.fields must be a non-empty string list")
        if len(fields) > 64:
            raise ProgramValidationError("select args.fields cannot exceed 64 entries")
        selected_rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ProgramValidationError("select input items must be objects")
            selected_rows.append({field: item[field] for field in fields if field in item})
        return selected_rows
    if op == "join":
        separator = args.get("separator", "\n")
        if not isinstance(separator, str) or len(separator) > 32:
            raise ProgramValidationError("join args.separator must be a string of at most 32 characters")
        if any(not isinstance(item, (str, int, float, bool)) and item is not None for item in items):
            raise ProgramValidationError("join input items must be JSON scalars")
        return separator.join("null" if item is None else str(item) for item in items)
    raise ProgramValidationError(f"unsupported transform op: {op}")


def _value_bytes(value: Any) -> tuple[bytes, str]:
    if isinstance(value, str):
        return value.encode("utf-8"), "text/plain"
    return _canonical_json(value).encode("utf-8"), "application/json"


def _action_result_status(result: str) -> str:
    lowered = str(result).strip().casefold()
    if "unknown outcome:" in lowered:
        return "unknown_outcome"
    if "timed out outcome:" in lowered:
        return "timed_out"
    if "cancelled outcome:" in lowered:
        return "cancelled"
    if lowered.startswith(("blocked by runtime authority", "blocked by runtime policy chain", "user denied")):
        return "denied"
    if lowered.startswith("skipped repeated failed attempt"):
        return "skipped"
    return classify_tool_status(result)


def _action_mutates_state(action: str) -> bool:
    try:
        return bool(get_action_spec(action).mutates_state)
    except KeyError:
        return False


def _apply_remaining_action_timeout(
    action: str,
    args: Mapping[str, Any],
    *,
    remaining_seconds: float,
) -> dict[str, Any]:
    """Clamp a timeout-aware action to the program's remaining budget."""

    resolved = dict(args)
    try:
        parameters = inspect.signature(TOOL_MAP[action]).parameters
    except (KeyError, TypeError, ValueError):
        return resolved
    if "timeout" not in parameters:
        return resolved

    requested = resolved.get("timeout", remaining_seconds)
    if (
        isinstance(requested, bool)
        or not isinstance(requested, (int, float))
        or not math.isfinite(float(requested))
        or float(requested) <= 0
    ):
        raise ProgramValidationError(f"{action} timeout must be a positive finite number")
    resolved["timeout"] = min(float(requested), max(0.001, remaining_seconds))
    return resolved


def _revalidate_compiled_program(
    compiled: CompiledProgram,
    *,
    cfg: Config,
    authorization: ProgramAuthorization,
    limits: ProgramLimits,
) -> CompiledProgram:
    """Recompile the frozen representation under current runtime authority."""

    try:
        normalized = _normalized_compiled_plan(compiled.steps, compiled.outputs)
        current = compile_program(
            normalized,
            authorization=authorization,
            limits=limits,
            cwd=cfg.cwd,
            safe_mode=bool(getattr(cfg, "safe_mode", True)),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, ProgramValidationError):
            raise
        raise ProgramValidationError("compiled program cannot be decoded safely") from exc
    if current != compiled:
        raise ProgramValidationError(
            "compiled program no longer matches the workspace, policy, schema, or authority ceiling"
        )
    return current


def _make_receipt(
    *,
    run_id: str,
    plan_hash: str,
    sequence: int,
    step_id: str,
    kind: str,
    operation: str,
    mutates_state: bool,
    verification_kind: str,
    status: str,
    input_hash: str,
    result_hash: str,
    result_bytes: int,
    artifact_uri: str,
    duration_ms: float,
    previous_hash: str,
) -> ProgramReceipt:
    if status not in _PROGRAM_STATUSES:
        raise ValueError(f"invalid receipt status: {status}")
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "plan_hash": plan_hash,
        "sequence": sequence,
        "step_id": step_id,
        "kind": kind,
        "operation": operation,
        "mutates_state": mutates_state,
        "verification_kind": verification_kind,
        "status": status,
        "input_hash": input_hash,
        "result_hash": result_hash,
        "result_bytes": result_bytes,
        "artifact_uri": artifact_uri,
        "duration_ms": round(max(0.0, float(duration_ms)), 3),
        "previous_hash": previous_hash,
    }
    return ProgramReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id=run_id,
        plan_hash=plan_hash,
        sequence=sequence,
        step_id=step_id,
        kind=kind,
        operation=operation,
        mutates_state=mutates_state,
        verification_kind=verification_kind,
        status=status,
        input_hash=input_hash,
        result_hash=result_hash,
        result_bytes=result_bytes,
        artifact_uri=artifact_uri,
        duration_ms=round(max(0.0, float(duration_ms)), 3),
        previous_hash=previous_hash,
        receipt_hash=_sha256_json(unsigned),
    )


def verify_receipt_chain(receipts: Sequence[ProgramReceipt]) -> bool:
    """Return whether a receipt sequence is complete and hash-linked."""

    previous = ZERO_RECEIPT_HASH
    run_id = receipts[0].run_id if receipts else ""
    plan_hash = receipts[0].plan_hash if receipts else ""
    for sequence, receipt in enumerate(receipts, 1):
        if (
            receipt.sequence != sequence
            or receipt.previous_hash != previous
            or receipt.run_id != run_id
            or receipt.plan_hash != plan_hash
            or receipt.receipt_hash != _sha256_json(receipt.unsigned_dict())
        ):
            return False
        previous = receipt.receipt_hash
    return True


def _program_output(
    reference: StepReference,
    value: Any,
    *,
    taint: ProgramTaint,
    limits: ProgramLimits,
    store: ProgramArtifactStore,
    artifacts_by_digest: dict[str, ArtifactRef],
) -> ProgramOutput:
    encoded, media_type = _value_bytes(value)
    digest = _sha256_bytes(encoded)
    rendered = ""
    artifact = artifacts_by_digest.get(digest)
    if artifact is None and (taint.protected or len(encoded) >= limits.artifact_threshold_bytes):
        artifact = store.put(encoded, media_type=media_type)
        artifacts_by_digest[digest] = artifact
    if taint.protected:
        preview = f"[protected output omitted; {len(encoded)} bytes encrypted]"
    elif media_type == "text/plain":
        rendered = value
        preview = rendered[: limits.output_preview_chars]
    else:
        rendered = _canonical_json(value)
        preview = rendered[: limits.output_preview_chars]
    if not taint.protected and len(rendered) > limits.output_preview_chars:
        preview += f"... [truncated; {len(encoded)} bytes total]"
    return ProgramOutput(
        reference,
        media_type,
        len(encoded),
        digest,
        preview,
        taint,
        artifact,
    )


def _program_value_identity(
    value: Any,
    *,
    taint: ProgramTaint,
    label: str,
) -> str:
    if taint.protected:
        return keyed_action_fingerprint(
            "program_value_identity",
            {"label": label, "value": value},
        )
    return _sha256_json(value)


def _program_is_cancelled(cancellation: DispatchCancellation | None) -> bool:
    return bool(cancellation is not None and cancellation.cancelled)


def execute_program(
    plan: Mapping[str, Any] | CompiledProgram,
    cfg: Config,
    *,
    authorization: ProgramAuthorization,
    limits: ProgramLimits = ProgramLimits(),
    store: ProgramArtifactStore | None = None,
    clock: Callable[[], float] = time.monotonic,
    dispatch_clock: Callable[[], float] = time.monotonic,
    cancellation: DispatchCancellation | None = None,
) -> ProgramResult:
    """Execute a compiled bounded program through Algo CLI's canonical tool path.

    The elapsed-time budget is enforced cooperatively: timeout-aware actions
    receive no more than the time remaining, and all actions are checked before
    and after dispatch.
    """

    if isinstance(plan, CompiledProgram):
        compiled = _revalidate_compiled_program(
            plan,
            cfg=cfg,
            authorization=authorization,
            limits=limits,
        )
    else:
        compiled = compile_program(
            plan,
            authorization=authorization,
            limits=limits,
            cwd=cfg.cwd,
            safe_mode=bool(getattr(cfg, "safe_mode", True)),
        )
    started = float(clock())
    if not math.isfinite(started) or started < 0:
        raise ValueError("clock must return a non-negative finite value")
    dispatch_started = float(dispatch_clock())
    if not math.isfinite(dispatch_started) or dispatch_started < 0:
        raise ValueError("dispatch_clock must return a non-negative finite value")
    dispatch_deadline = dispatch_started + float(limits.max_runtime_seconds)
    artifact_store = store or ProgramArtifactStore()
    run_id = uuid.uuid4().hex
    artifact_store.begin_run(run_id)
    values: dict[str, Any] = {}
    taint_by_step = {step.step_id: step.output_taint for step in compiled.steps}
    artifacts_by_digest: dict[str, ArtifactRef] = {}
    receipts: list[ProgramReceipt] = []
    cumulative_bytes = 0
    status = "worked"
    error = ""
    last_clock = started
    own_scope: execution_guardrails.ExecutionScope | None = None
    active_workspace = execution_guardrails.active_workspace()
    if active_workspace is None:
        own_scope = execution_guardrails.begin_execution_scope(cfg.cwd)
    else:
        try:
            configured_workspace = Path(cfg.cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProgramValidationError("configured workspace cannot be resolved") from exc
        if configured_workspace != active_workspace:
            raise ProgramValidationError("active execution scope does not match the configured workspace")

    try:
        for sequence, step in enumerate(compiled.steps, 1):
            now = float(clock())
            if not math.isfinite(now):
                raise ValueError("clock must return finite values")
            pre_step_status = ""
            if now < last_clock:
                pre_step_status = "failed"
                error = f"program clock regressed before {step.step_id}"
            elif _program_is_cancelled(cancellation):
                pre_step_status = "cancelled"
                error = f"program was cancelled before {step.step_id}"
            elapsed = now - started
            if not pre_step_status and elapsed >= limits.max_runtime_seconds:
                pre_step_status = "limit_exceeded"
                error = f"program runtime exceeded {limits.max_runtime_seconds:.3f} seconds before {step.step_id}"
            if pre_step_status:
                status = pre_step_status
                encoded, media_type = _value_bytes(error)
                limit_artifact = (
                    artifact_store.put(encoded, media_type=media_type)
                    if len(encoded) >= limits.artifact_threshold_bytes
                    else None
                )
                receipt = _make_receipt(
                    run_id=run_id,
                    plan_hash=compiled.plan_hash,
                    sequence=sequence,
                    step_id=step.step_id,
                    kind=step.kind,
                    operation=step.action if isinstance(step, ActionProgramStep) else step.op,
                    mutates_state=_action_mutates_state(step.action) if isinstance(step, ActionProgramStep) else False,
                    verification_kind="",
                    status=status,
                    input_hash=_sha256_json(None),
                    result_hash=_sha256_bytes(encoded),
                    result_bytes=len(encoded),
                    artifact_uri=limit_artifact.uri if limit_artifact else "",
                    duration_ms=0.0,
                    previous_hash=receipts[-1].receipt_hash if receipts else ZERO_RECEIPT_HASH,
                )
                receipts.append(receipt)
                break
            last_clock = now

            step_started = now
            operation = step.action if isinstance(step, ActionProgramStep) else step.op
            mutates_state = _action_mutates_state(operation) if isinstance(step, ActionProgramStep) else False
            resolved_input: Any = None
            value: Any
            step_status = "worked"
            verification_kind = ""
            evidence_before = len(execution_guardrails.evidence_snapshot())
            try:
                if isinstance(step, ActionProgramStep):
                    resolved_input = _resolve_refs(step.args(), values)
                    if not isinstance(resolved_input, dict):
                        raise ProgramValidationError(f"resolved args for {step.step_id} are not an object")
                    resolved_input = _apply_remaining_action_timeout(
                        step.action,
                        resolved_input,
                        remaining_seconds=max(0.001, limits.max_runtime_seconds - elapsed),
                    )
                    dispatched = execute_tool_call_for_pipeline(
                        step.action,
                        resolved_input,
                        cfg,
                        tool_call_id=f"program-{run_id[:8]}-{step.step_id}",
                        force_approval=step.action in authorization.force_approval_actions,
                        deadline_monotonic=dispatch_deadline,
                        cancellation=cancellation,
                    )
                    _message, action_result = dispatched
                    value = action_result
                    typed_outcome = getattr(dispatched, "outcome", None)
                    if typed_outcome is None:
                        step_status = _action_result_status(action_result)
                    elif typed_outcome.status.value == "succeeded":
                        step_status = "worked"
                    else:
                        step_status = typed_outcome.status.value
                else:
                    resolved_input = {
                        "input": _resolve_refs(step.input_value(), values),
                        "args": _resolve_refs(step.args(), values),
                    }
                    if not isinstance(resolved_input["args"], dict):
                        raise ProgramValidationError(f"resolved transform args for {step.step_id} are not an object")
                    value = _deterministic_transform(
                        step.op,
                        resolved_input["input"],
                        resolved_input["args"],
                        limits,
                    )
                    if _program_is_cancelled(cancellation):
                        value = "Program cancelled."
                        step_status = "cancelled"
            except ProgramValidationError as exc:
                value = f"Program step error: {exc}"
                step_status = "failed"
            except Exception as exc:
                value = f"Program step error: {type(exc).__name__}"
                step_status = "failed"

            encoded, media_type = _value_bytes(value)
            digest = _sha256_bytes(encoded)
            step_taint = step.output_taint
            artifact: ArtifactRef | None = None
            if step_taint.protected or len(encoded) >= limits.artifact_threshold_bytes:
                artifact = artifact_store.put(encoded, media_type=media_type)
                artifacts_by_digest[digest] = artifact
            cumulative_bytes += len(encoded)
            finished = float(clock())
            if not math.isfinite(finished):
                raise ValueError("clock must return finite values")
            program_stop_status = ""
            if finished < last_clock:
                program_stop_status = "failed"
                error = f"program clock regressed during {step.step_id}"
            elif finished - started > limits.max_runtime_seconds:
                program_stop_status = "limit_exceeded"
                error = f"program runtime exceeded {limits.max_runtime_seconds:.3f} seconds during {step.step_id}"
            elif cumulative_bytes > limits.max_intermediate_bytes:
                program_stop_status = "limit_exceeded"
                error = (
                    f"program intermediate values reached {cumulative_bytes} bytes; "
                    f"maximum is {limits.max_intermediate_bytes}"
                )
            elif step_status != "worked":
                error = (
                    f"{operation} returned {step_status}; protected details were omitted"
                    if step_taint.protected
                    else str(value)
                )
            last_clock = max(last_clock, finished)

            # A typed program must not depend on an incidental nested side
            # effect to satisfy the outer completion gate. Classify a passing
            # run_shell verifier deterministically, encode it in the hash-linked
            # receipt, and reconcile it into the active ledger if the canonical
            # nested dispatch did not already do so.
            if (
                isinstance(step, ActionProgramStep)
                and step.action == "run_shell"
                and step_status == "worked"
                and _SUCCESSFUL_SHELL_RESULT_RE.search(str(value))
            ):
                verification = execution_guardrails.classify_verification_command(
                    str(resolved_input.get("command") or "")
                )
                if verification.qualifies and verification.kind:
                    verification_kind = verification.kind
                    new_evidence = execution_guardrails.evidence_snapshot()[evidence_before:]
                    if not any(
                        event.kind == "verification" and event.verification_kind == verification_kind
                        for event in new_evidence
                    ):
                        execution_guardrails.record_verification(
                            verification_kind,
                            success=True,
                        )

            receipt_status = step_status
            if program_stop_status and step_status == "worked" and not mutates_state:
                receipt_status = program_stop_status
            input_identity = _program_value_identity(
                resolved_input,
                taint=step_taint,
                label=f"{step.step_id}:input",
            )
            result_identity = artifact.content_id if step_taint.protected and artifact is not None else digest
            receipt = _make_receipt(
                run_id=run_id,
                plan_hash=compiled.plan_hash,
                sequence=sequence,
                step_id=step.step_id,
                kind=step.kind,
                operation=operation,
                mutates_state=mutates_state,
                verification_kind=verification_kind,
                status=receipt_status,
                input_hash=input_identity,
                result_hash=result_identity,
                result_bytes=len(encoded),
                artifact_uri=artifact.uri if artifact else "",
                duration_ms=max(0.0, finished - step_started) * 1000,
                previous_hash=receipts[-1].receipt_hash if receipts else ZERO_RECEIPT_HASH,
            )
            receipts.append(receipt)
            if step_status != "worked":
                status = step_status
                break
            values[step.step_id] = value
            if program_stop_status:
                status = program_stop_status
                break

        outputs: tuple[ProgramOutput, ...] = ()
        if status == "worked":
            outputs = tuple(
                _program_output(
                    reference,
                    _resolve_reference(reference, values),
                    taint=taint_by_step[reference.step_id],
                    limits=limits,
                    store=artifact_store,
                    artifacts_by_digest=artifacts_by_digest,
                )
                for reference in compiled.outputs
            )
        if not verify_receipt_chain(receipts):
            raise ProgramStoreError("program receipt chain failed self-verification")
        receipt_uri = artifact_store.write_receipts(run_id, receipts)
        chain_hash = receipts[-1].receipt_hash if receipts else ZERO_RECEIPT_HASH
        requires_reconciliation = any(receipt.status == "unknown_outcome" for receipt in receipts)
        return ProgramResult(
            status=status,
            run_id=run_id,
            plan_hash=compiled.plan_hash,
            outputs=outputs,
            receipts=tuple(receipts),
            receipt_chain_hash=chain_hash,
            receipt_uri=receipt_uri,
            error=error,
            requires_reconciliation=requires_reconciliation,
        )
    finally:
        if own_scope is not None:
            execution_guardrails.end_execution_scope(own_scope)


__all__ = [
    "ActionProgramStep",
    "ArtifactRef",
    "CompiledProgram",
    "ProgramArtifactStore",
    "ProgramAuthorization",
    "ProgramLimits",
    "ProgramOutput",
    "ProgramReceipt",
    "ProgramResult",
    "ProgramStoreError",
    "ProgramValidationError",
    "StepReference",
    "TransformProgramStep",
    "authorization_for_actions",
    "compile_program",
    "execute_program",
    "verify_receipt_chain",
]
