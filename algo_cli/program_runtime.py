"""Bounded typed programs over the existing Algo CLI action runtime.

This module intentionally does not execute model-authored Python or JavaScript.
It compiles a small JSON plan whose action steps must pass the same policy,
approval, attempt-ledger, and execution-guardrail path as ordinary tool calls.
Pure transforms operate only on JSON-compatible values produced by earlier
steps.  Large values are content-addressed outside the conversation and every
step produces a frozen, hash-chained receipt.

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
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import execution_guardrails
from .action_registry import get_action_spec
from .config import CONFIG_DIR, Config
from .tool_runtime import classify_tool_status, execute_tool_call_for_pipeline
from .tools import TOOL_MAP


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
_PROGRAM_STATUSES = frozenset({"worked", "failed", "denied", "skipped", "limit_exceeded"})
_SUCCESSFUL_SHELL_RESULT_RE = re.compile(r"\[exit code:\s*0\]", re.IGNORECASE)


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
        str(name).strip()
        for name in action_names
        if str(name).strip() and str(name).strip() not in _FORBIDDEN_ACTIONS
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
class ActionProgramStep:
    step_id: str
    action: str
    args_json: str
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


@dataclass(frozen=True)
class ArtifactRef:
    uri: str
    digest: str
    byte_count: int
    media_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "sha256": self.digest,
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
    artifact: ArtifactRef | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reference": self.reference.to_dict(),
            "media_type": self.media_type,
            "bytes": self.byte_count,
            "sha256": self.sha256,
            "preview": self.preview,
        }
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


def compile_program(
    plan: Mapping[str, Any],
    *,
    authorization: ProgramAuthorization,
    limits: ProgramLimits = ProgramLimits(),
) -> CompiledProgram:
    """Validate and freeze a model-authored typed program before any action runs."""

    if not isinstance(plan, Mapping):
        raise ProgramValidationError("program must be a JSON object")
    plan_dict = dict(plan)
    plan_bytes = len(_canonical_json(plan_dict).encode("utf-8"))
    if plan_bytes > limits.max_plan_bytes:
        raise ProgramValidationError(
            f"program is {plan_bytes} bytes; maximum is {limits.max_plan_bytes}"
        )
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
            compiled_steps.append(ActionProgramStep(step_id, action, _canonical_json(args)))
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
            compiled_steps.append(
                TransformProgramStep(
                    step_id,
                    op,
                    _canonical_json(transform_input),
                    _canonical_json(args),
                )
            )
        else:
            raise ProgramValidationError(f"{location}.kind must be 'action' or 'transform'")
        available.add(step_id)

    raw_outputs = plan_dict.get("outputs")
    if raw_outputs is None:
        raw_outputs = [compiled_steps[-1].step_id]
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ProgramValidationError("program outputs must be a non-empty list")
    if len(raw_outputs) > limits.max_outputs:
        raise ProgramValidationError(
            f"program has {len(raw_outputs)} outputs; maximum is {limits.max_outputs}"
        )
    all_steps = frozenset(available)
    outputs = tuple(
        _parse_reference(value, available=all_steps, location=f"outputs[{index}]")
        for index, value in enumerate(raw_outputs)
    )
    plan_hash = _sha256_json(plan_dict)
    return CompiledProgram(PROGRAM_SCHEMA_VERSION, plan_hash, tuple(compiled_steps), outputs)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ProgramStoreError("program store directories must be real directories")
    if os.name == "posix":
        os.chmod(path, 0o700)


class ProgramArtifactStore:
    """Content-addressed blobs plus atomically published per-run receipt ledgers."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = Path(root) if root is not None else CONFIG_DIR / "private" / "program_runtime"
        self.root = configured.expanduser().resolve(strict=False)
        _ensure_private_directory(self.root)
        _ensure_private_directory(self.root / "artifacts")
        _ensure_private_directory(self.root / "receipts")

    def _artifact_path(self, digest: str) -> Path:
        if not _HASH_RE.fullmatch(digest):
            raise ProgramStoreError("artifact digest is invalid")
        bucket = self.root / "artifacts" / digest[:2]
        _ensure_private_directory(bucket)
        return bucket / f"{digest}.blob"

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
        self._atomic_publish_new(self._artifact_path(digest), content)
        return ArtifactRef(f"artifact://sha256/{digest}", digest, len(content), media_type)

    def read(self, ref: ArtifactRef) -> bytes:
        content = self._artifact_path(ref.digest).read_bytes()
        if len(content) != ref.byte_count or _sha256_bytes(content) != ref.digest:
            raise ProgramStoreError("artifact integrity check failed")
        return content

    def write_receipts(self, run_id: str, receipts: Sequence[ProgramReceipt]) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ProgramStoreError("run id is invalid")
        content = b"".join(
            (_canonical_json(receipt.to_dict()) + "\n").encode("utf-8") for receipt in receipts
        )
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
                raise ProgramValidationError(
                    f"reference {reference.step_id} path index {component} is unavailable"
                )
            current = current[component]
        else:
            if not isinstance(current, dict) or component not in current:
                raise ProgramValidationError(
                    f"reference {reference.step_id} path key {component!r} is unavailable"
                )
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
        raise ProgramValidationError(
            f"{op} input has {len(value)} items; maximum is {limits.max_collection_items}"
        )
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
    lowered = str(result).strip().lower()
    if lowered.startswith(("blocked by runtime policy chain", "user denied")):
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
    limits: ProgramLimits,
    store: ProgramArtifactStore,
    artifacts_by_digest: dict[str, ArtifactRef],
) -> ProgramOutput:
    encoded, media_type = _value_bytes(value)
    digest = _sha256_bytes(encoded)
    artifact = artifacts_by_digest.get(digest)
    if artifact is None and len(encoded) >= limits.artifact_threshold_bytes:
        artifact = store.put(encoded, media_type=media_type)
        artifacts_by_digest[digest] = artifact
    if media_type == "text/plain":
        rendered = value
    else:
        rendered = _canonical_json(value)
    preview = rendered[: limits.output_preview_chars]
    if len(rendered) > limits.output_preview_chars:
        preview += f"... [truncated; {len(encoded)} bytes total]"
    return ProgramOutput(reference, media_type, len(encoded), digest, preview, artifact)


def execute_program(
    plan: Mapping[str, Any] | CompiledProgram,
    cfg: Config,
    *,
    authorization: ProgramAuthorization,
    limits: ProgramLimits = ProgramLimits(),
    store: ProgramArtifactStore | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProgramResult:
    """Execute a compiled bounded program through Algo CLI's canonical tool path.

    The elapsed-time budget is enforced cooperatively: timeout-aware actions
    receive no more than the time remaining, and all actions are checked before
    and after dispatch.
    """

    compiled = plan if isinstance(plan, CompiledProgram) else compile_program(
        plan,
        authorization=authorization,
        limits=limits,
    )
    if len(compiled.steps) > limits.max_steps:
        raise ProgramValidationError("compiled program exceeds the current max_steps ceiling")
    for step in compiled.steps:
        if isinstance(step, ActionProgramStep) and step.action not in authorization.allowed_actions:
            raise ProgramValidationError(f"compiled action is outside the current capability ceiling: {step.action}")

    artifact_store = store or ProgramArtifactStore()
    run_id = uuid.uuid4().hex
    started = float(clock())
    if not math.isfinite(started):
        raise ValueError("clock must return finite values")
    values: dict[str, Any] = {}
    artifacts_by_digest: dict[str, ArtifactRef] = {}
    receipts: list[ProgramReceipt] = []
    cumulative_bytes = 0
    status = "worked"
    error = ""
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
            elapsed = now - started
            if elapsed >= limits.max_runtime_seconds:
                status = "limit_exceeded"
                error = f"program runtime exceeded {limits.max_runtime_seconds:.3f} seconds before {step.step_id}"
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
                    _message, action_result = execute_tool_call_for_pipeline(
                        step.action,
                        resolved_input,
                        cfg,
                        tool_call_id=f"program-{run_id[:8]}-{step.step_id}",
                        force_approval=step.action in authorization.force_approval_actions,
                    )
                    value = action_result
                    step_status = _action_result_status(action_result)
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
            except ProgramValidationError as exc:
                value = f"Program step error: {exc}"
                step_status = "failed"
            except Exception as exc:
                value = f"Program step error: {type(exc).__name__}: {exc}"
                step_status = "failed"

            encoded, media_type = _value_bytes(value)
            digest = _sha256_bytes(encoded)
            artifact: ArtifactRef | None = None
            if len(encoded) >= limits.artifact_threshold_bytes:
                artifact = artifact_store.put(encoded, media_type=media_type)
                artifacts_by_digest[digest] = artifact
            cumulative_bytes += len(encoded)
            finished = float(clock())
            if not math.isfinite(finished):
                raise ValueError("clock must return finite values")
            if finished - started > limits.max_runtime_seconds:
                step_status = "limit_exceeded"
                error = f"program runtime exceeded {limits.max_runtime_seconds:.3f} seconds during {step.step_id}"
            elif cumulative_bytes > limits.max_intermediate_bytes:
                step_status = "limit_exceeded"
                error = (
                    f"program intermediate values reached {cumulative_bytes} bytes; "
                    f"maximum is {limits.max_intermediate_bytes}"
                )
            elif step_status != "worked":
                error = str(value)

            # A typed program must not depend on an incidental nested side
            # effect to satisfy the outer completion gate. Classify a passing
            # run_shell verifier deterministically, encode it in the immutable
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
                        event.kind == "verification"
                        and event.verification_kind == verification_kind
                        for event in new_evidence
                    ):
                        execution_guardrails.record_verification(
                            verification_kind,
                            success=True,
                        )

            receipt = _make_receipt(
                run_id=run_id,
                plan_hash=compiled.plan_hash,
                sequence=sequence,
                step_id=step.step_id,
                kind=step.kind,
                operation=operation,
                mutates_state=mutates_state,
                verification_kind=verification_kind,
                status=step_status,
                input_hash=_sha256_json(resolved_input),
                result_hash=digest,
                result_bytes=len(encoded),
                artifact_uri=artifact.uri if artifact else "",
                duration_ms=(finished - step_started) * 1000,
                previous_hash=receipts[-1].receipt_hash if receipts else ZERO_RECEIPT_HASH,
            )
            receipts.append(receipt)
            if step_status != "worked":
                status = step_status
                break
            values[step.step_id] = value

        outputs: tuple[ProgramOutput, ...] = ()
        if status == "worked":
            outputs = tuple(
                _program_output(
                    reference,
                    _resolve_reference(reference, values),
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
        return ProgramResult(
            status,
            run_id,
            compiled.plan_hash,
            outputs,
            tuple(receipts),
            chain_hash,
            receipt_uri,
            error,
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
