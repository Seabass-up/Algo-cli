"""Native SSEA static inspectors through a pinned, default-deny REA boundary.

SSEA is Algo's name; upstream rea-agents remains MIT, https://github.com/morluto/rea.
Only the reviewed target-free subset below is executable. Target code never runs.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import time
from typing import Any

from . import ssea_sandbox as boundary
from .irene_memory_path_policy import ProtectedMemoryPathError, require_allowed_path

INTEGRATION_NAME = "study-software-engineer-anything"
REVIEWED_VERSION = "6.8.0"
MAX_STEPS = 4
SSEA_INSPECTION_TOOLS = frozenset({"ssea_analyze", "ssea_investigate"})
_RESULT_BUDGET = 18_000  # Leave headroom below the model tool-content cap.
# (minimum, maximum, default). Algo deliberately narrows upstream maxima.
OPERATIONS = {
    "inspect_python_pyc": {
        "max_file_bytes": (16, boundary.MAX_INPUT, boundary.MAX_INPUT),
        "max_objects": (1, 16384, 4096), "max_depth": (1, 64, 32),
        "max_string_bytes": (1, 1048576, 65536),
    },
    "inspect_elf_notes": {
        "max_file_bytes": (64, boundary.MAX_INPUT, boundary.MAX_INPUT),
        "max_entries": (1, 256, 32),
    },
    "inspect_pe_imports": {
        "max_file_bytes": (64, boundary.MAX_INPUT, boundary.MAX_INPUT),
        "max_dlls": (1, 256, 64), "max_functions": (1, 8192, 1024),
    },
    "inspect_native_exports": {"max_file_bytes": (64, boundary.MAX_INPUT, boundary.MAX_INPUT)},
}
LIMITS = {"input_bytes": boundary.MAX_INPUT, "output_bytes_per_step": boundary.MAX_OUTPUT,
          "workflow_seconds": 60, "step_seconds": 30, "max_steps": MAX_STEPS,
          "child_processes": "denied", "network": "denied", "target_execution": "denied"}


def _blocked(error: str, status: str = "policy_blocked") -> dict[str, Any]:
    return {"ok": False, "status": status, "error": error, "verified": False}


def _options(operation: Any, options: Any) -> dict[str, int]:
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise boundary.BoundaryError("ssea_unknown_or_unqualified_operation")
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - set(OPERATIONS[operation]):
        raise boundary.BoundaryError("ssea_invalid_options")
    validated = {}
    for name, (minimum, maximum, default) in OPERATIONS[operation].items():
        value = options.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise boundary.BoundaryError("ssea_invalid_option")
        validated[name] = value
    return validated


def status(cfg: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"integration": INTEGRATION_NAME, "upstream": "rea-agents",
                              "reviewed_version": REVIEWED_VERSION, "license": "MIT", "limits": LIMITS}
    try:
        root, _node = boundary.installation(cfg)
        metadata = require_allowed_path(root / "package.json", cwd=root)
        if metadata.stat().st_size > 65536:
            raise boundary.BoundaryError("ssea_metadata_too_large")
        with boundary.regular_reader(metadata) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise boundary.BoundaryError("ssea_metadata_too_large")
        package = json.loads(raw)
        if not isinstance(package, dict) or package.get("name") != "rea-agents":
            raise boundary.BoundaryError("ssea_invalid_package_metadata")
        version = package.get("version")
        if not isinstance(version, str) or not version.strip() or len(version) > 128:
            raise boundary.BoundaryError("ssea_invalid_package_metadata")
        result["installed_version"] = version
        result["package_evidence"] = "metadata_only_not_readiness"
        result["execution"] = "requires_pinned_runtime_and_containment_preflight"
        result["ok"] = True
    except (OSError, ValueError, RecursionError, boundary.BoundaryError, ProtectedMemoryPathError):
        result.update(_blocked("ssea_runtime_metadata_unavailable"))
    return result


def catalog(cfg: Any = None) -> dict[str, Any]:
    return {"integration": INTEGRATION_NAME, "ok": True, "upstream": "rea-agents",
            "version": REVIEWED_VERSION, "scope": "macOS pinned target-free static subset",
            "operations": [{"name": name, "upstream_cli": name.replace("_", "-"),
                            "parameters": {key: {"type": "integer", "minimum": spec[0],
                                                  "maximum": spec[1], "default": spec[2]}
                                           for key, spec in options.items()},
                            "effects": "contained_code_execution_on_read_only_snapshot",
                            "requires_approval": True}
                           for name, options in OPERATIONS.items()],
            "unsupported": ["inspect_format", "extract_strings", "disassemble", "analyze_structure",
                            "trace_data_flow", "debugger", "target_execution", "downloads", "network"],
            "limits": LIMITS, "evidence": "installed_canonical_schemas_and_cli_source_review"}


def _execute(operation: str, values: dict[str, int], root: Path, node: Path, scratch: Path,
             identity: dict[str, Any], cfg: Any, deadline: float) -> dict[str, Any]:
    args = [operation.replace("_", "-"), str(scratch / "target.bin"), "--format", "json"]
    for key, value in values.items():
        args.extend(("--" + key.replace("_", "-"), str(value)))
    result = boundary.run(root, node, scratch, args, cfg, min(deadline, time.monotonic() + 30))
    if result.cancelled or result.timed_out:
        return _blocked("ssea_cancelled" if result.cancelled else "ssea_timeout", "cancelled" if result.cancelled else "timed_out")
    if result.truncated or result.stderr_truncated:
        return _blocked("ssea_output_limit", "unknown_outcome")
    if result.returncode != 0:
        return _blocked("ssea_upstream_process_failed", "provider_error")
    try:
        evidence = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _blocked("ssea_invalid_upstream_json", "unknown_outcome")
    if not isinstance(evidence, dict) or "error" in evidence or "evidence_id" not in evidence:
        return {**_blocked("ssea_upstream_rejected_input", "provider_error"),
                "upstream_error": evidence if isinstance(evidence, dict) else None}
    subject = evidence.get("subject")
    digest = subject.get("digest") if isinstance(subject, dict) else None
    if (evidence.get("operation") != operation or not isinstance(digest, dict) or
            digest.get("sha256") != identity["sha256"]):
        return _blocked("ssea_evidence_identity_mismatch", "unknown_outcome")
    # Bind delivered Evidence to the selected immutable snapshot, without
    # upgrading provider analysis or static observations into runtime truth.
    if hashlib.sha256((scratch / "target.bin").read_bytes()).hexdigest() != identity["sha256"]:
        return _blocked("ssea_snapshot_changed", "unknown_outcome")
    return {"ok": True, "status": "executed", "operation": operation, "input": identity,
            "evidence": evidence, "upstream_result_verified": False,
            "containment": "macOS-seatbelt-default-deny-no-fork-no-network",
            "untrusted_content": True, "limits": LIMITS,
            "limitations": ["Static observations do not establish runtime behavior.",
                            "Evidence refers to an isolated copy; original identity is bound by SHA256."]}


def investigate(target: Any, steps: Any, cfg: Any = None) -> dict[str, Any]:
    # Validate the complete plan before reading or executing any target.
    try:
        if not isinstance(target, str) or not target or "\x00" in target:
            raise boundary.BoundaryError("ssea_invalid_target")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise boundary.BoundaryError("ssea_invalid_steps")
        planned = []
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"operation", "options"}:
                raise boundary.BoundaryError("ssea_invalid_step")
            operation = step.get("operation")
            values = _options(operation, step.get("options"))
            if not isinstance(operation, str):
                raise boundary.BoundaryError("ssea_invalid_operation")
            planned.append((operation, values))
        deadline = time.monotonic() + 60
        # Capture once; integrity checks and every step share the workflow budget.
        root, node = boundary.verify_runtime(cfg, deadline)
        parent = Path(tempfile.gettempdir()).resolve()
        require_allowed_path(parent, cwd=parent)
        with tempfile.TemporaryDirectory(prefix="algo-ssea-", dir=parent) as directory:
            scratch = Path(directory)
            identity = boundary.snapshot(target, scratch / "target.bin", getattr(cfg, "cwd", str(Path.cwd())))
            results = []
            for index, (operation, values) in enumerate(planned):
                result = _execute(operation, values, root, node, scratch, identity, cfg, deadline)
                results.append(result)
                if result["status"] != "executed":
                    payload = {"ok": False, "status": "stopped", "stopped_at_step": index, "steps": results}
                    break
            else:
                payload = {"ok": True, "status": "executed", "steps": results}
        payload["cleanup"] = "process_reaped_scratch_removed"
        return payload
    except ProtectedMemoryPathError:
        return _blocked("ssea_protected_or_aliased_path")
    except boundary.BoundaryError as exc:
        return _blocked(str(exc))
    except (OSError, ValueError, RuntimeError):
        return _blocked("ssea_boundary_or_cleanup_failure", "unknown_outcome")


def analyze(operation: Any, target: Any, options: Any = None, cfg: Any = None) -> dict[str, Any]:
    payload = investigate(target, [{"operation": operation, "options": options}], cfg)
    if payload.get("steps"):
        return {**payload["steps"][0], "cleanup": payload.get("cleanup")}
    return payload


def serialize_result(payload: dict[str, Any]) -> str:
    """Keep workflow and step outcomes intact when parser detail cannot fit."""
    def encode(value: dict[str, Any]) -> str:
        return json.dumps(value, separators=(",", ":"), allow_nan=False)

    try:
        text = encode(payload)
        if len(text) <= _RESULT_BUDGET:
            return text
    except (RecursionError, ValueError):
        pass  # Non-JSON provider data must not obscure the action outcome.

    def compact(step: dict[str, Any]) -> dict[str, Any]:
        summary = {key: value for key, value in step.items() if key not in {"evidence", "upstream_error", "steps"}}
        evidence = step.get("evidence")
        if isinstance(evidence, dict):
            evidence_id = evidence.get("evidence_id")
            if isinstance(evidence_id, str) and len(evidence_id) <= 128:
                summary["evidence_id"] = evidence_id
        if "evidence" in step or "upstream_error" in step:
            summary["detail_omitted"] = True
        identity = summary.get("input")
        if isinstance(identity, dict) and len(encode(identity)) > 1024:
            summary["input"] = {key: identity[key] for key in ("sha256", "bytes")}
            summary["target_omitted"] = True
        return summary

    result = compact(payload)
    if "steps" in payload:
        result["steps"] = [compact(step) for step in payload["steps"]]
    result["detail_omitted"] = True
    for index, step in enumerate(payload.get("steps", ())):
        summary = result["steps"][index]
        result["steps"][index] = step
        try:
            if len(encode(result)) <= _RESULT_BUDGET:
                continue
        except (RecursionError, ValueError):
            pass
        result["steps"][index] = summary
    return encode(result)


def parse_result(text: str) -> dict[str, Any] | None:
    if len(text) > _RESULT_BUDGET or not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(payload, dict) and type(payload.get("ok")) is bool and isinstance(payload.get("status"), str):
        return payload
    return None


def ssea_status(cfg: Any = None) -> str:
    """Report SSEA installation metadata, static scope and execution preflight requirements."""
    return json.dumps(status(cfg), ensure_ascii=False)


def ssea_catalog(cfg: Any = None) -> str:
    """List exact supported SSEA static inspectors, typed limits and unsupported workflows."""
    return json.dumps(catalog(cfg), ensure_ascii=False)


def ssea_analyze(operation: str, target: str, options: dict[str, Any] | None = None, cfg: Any = None) -> str:
    """Run one approved SSEA/REA static inspector in a default-deny OS sandbox.

    Args:
        operation: Exact operation from ssea_catalog; not an arbitrary tool name.
        target: Explicit regular, unaliased, nonprotected input file (up to 16 MiB).
        options: Only bounded integer options listed in ssea_catalog.
    """
    return serialize_result(analyze(operation, target, options, cfg))


def ssea_investigate(target: str, steps: list[dict[str, Any]], cfg: Any = None) -> str:
    """Run up to four approved SSEA static inspections on one identical input snapshot.

    Args:
        target: Explicit regular, unaliased, nonprotected input file.
        steps: Ordered operation/options objects from ssea_catalog; stops on first failure.
    """
    return serialize_result(investigate(target, steps, cfg))
