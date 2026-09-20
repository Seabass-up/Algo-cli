"""Explicit D-57 CLI memory authority; never a plaintext fallback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


class D057MemoryError(RuntimeError):
    """The selected D-57 bridge could not safely complete an operation."""


def selected(cfg: Any) -> bool:
    enabled = getattr(cfg, "d057_enabled", False)
    if type(enabled) is not bool:
        raise D057MemoryError("Invalid D-57 authority selection.")
    if enabled and (
        cfg.echo_veil_enabled or str(cfg.echo_veil_protection).strip().casefold() == "required"
    ):
        raise D057MemoryError("Conflicting D-57 and Echo memory authorities; choose one explicitly.")
    return enabled


def _task_id() -> str:
    from . import config

    namespace = hashlib.sha256(str(config.CONFIG_DIR.resolve()).encode()).hexdigest()[:24]
    return f"algo-cli-memory-{namespace}"


def _run(cfg: Any, args: list[str]) -> dict[str, Any]:
    if not selected(cfg):
        raise D057MemoryError("D-57 memory is not selected.")
    try:
        root = Path(cfg.d057_package_root).expanduser().resolve(strict=True)
        cli = Path(cfg.d057_cli).expanduser().resolve(strict=True)
        expected = root / "algo-package" / "bin" / "d057_algo_cli.py"
        if not cfg.d057_package_root or not cfg.d057_cli or cli != expected or not cli.is_file():
            raise ValueError("unexpected bridge")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["D057_CLI"] = str(root / "pi-package" / "bin" / "d057_pi_cli.py")
        env["D057_PYTHON"] = sys.executable
        result = subprocess.run(
            [sys.executable, "-I", str(cli), *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
            cwd=root,
        )
        if len(result.stdout.encode("utf-8")) > 128_000:
            raise ValueError("oversized reply")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("unexpected reply")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise D057MemoryError("D-57 bridge unavailable; no host memory fallback was used.") from exc
    if result.returncode != 0 or payload.get("ok") is not True:
        if args[0] == "recall" and payload.get("ok") is False and payload.get("error") in {"not_found", "empty"}:
            return {"ok": False, "error": payload["error"]}
        raise D057MemoryError("D-57 refused the operation; no host memory fallback was used.")
    return payload


def doctor(cfg: Any) -> dict[str, Any]:
    payload = _run(cfg, ["doctor"])
    tip = payload.get("tip_sequence")
    if payload.get("verify") is not True or type(tip) is not int or tip < 0:
        raise D057MemoryError("D-57 store verification failed; no host memory fallback was used.")
    return {
        "ok": True,
        "adapter": "d057-algo",
        "verify": True,
        "tip_sequence": payload.get("tip_sequence"),
    }


def recall_facts(cfg: Any) -> list[str]:
    from .julia_memory_runtime import _validate_content

    health = doctor(cfg)
    payload = _run(cfg, ["recall", "--task-id", _task_id()])
    if payload.get("error") == "not_found" or (payload.get("error") == "empty" and health["tip_sequence"] == 0):
        cfg.memories = []
        return []
    if payload.get("error"):
        raise D057MemoryError("D-57 refused memory recall; no host memory fallback was used.")
    projection = payload.get("projection")
    try:
        if not isinstance(projection, dict):
            raise ValueError("invalid projection")
        capsule = projection["capsule"]
        semantic = capsule["summary"]
        if capsule["task_id"] != _task_id() or semantic["schema"] != "algo-cli-facts-v1":
            raise ValueError("unexpected projection")
        facts = semantic["facts"]
        if not isinstance(facts, list) or any(type(fact) is not str for fact in facts):
            raise ValueError("invalid facts")
        validated = [_validate_content(fact) for fact in facts]
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise D057MemoryError("D-57 returned an invalid memory projection.") from exc
    cfg.memories = list(dict.fromkeys(validated))
    return list(cfg.memories)


def remember_fact(cfg: Any, fact: str) -> bool:
    from . import config
    from .julia_memory_runtime import _validate_content

    fact = _validate_content(fact)
    # D-57 projections select the latest snapshot. Serialize Algo read/append
    # transactions across processes so concurrent facts cannot overwrite one another.
    with config._exclusive_state_lock(config.CONFIG_DIR / "d057-memory-transaction"):
        facts = recall_facts(cfg)
        if fact in facts:
            return False
        semantic = {"schema": "algo-cli-facts-v1", "facts": [*facts, fact]}
        encoded = json.dumps(semantic, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 20_000:
            raise D057MemoryError("D-57 memory snapshot is full; no facts were discarded.")
        _run(cfg, ["remember", "--task-id", _task_id(), "--semantic", encoded])
        # Never retry an uncertain append. A verified readback must match it.
        if recall_facts(cfg) != semantic["facts"]:
            raise D057MemoryError("D-57 memory write could not be verified; do not automatically retry.")
    return True
