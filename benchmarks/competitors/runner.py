#!/usr/bin/env python3
"""Reproducible, fail-closed benchmark runner for terminal agent harnesses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
TASK_ROOT = HERE / "tasks"
DEFAULT_MODEL = "qwen3.6:35b-mlx"
DEFAULT_TIMEOUT = 360
SCHEMA_VERSION = 1
PROTOCOL = "algo-cli-cross-harness-v2-draft"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    allowed_workspace_changes: frozenset[str]
    protected_workspace_paths: frozenset[str]


@dataclass(frozen=True)
class ProductSpec:
    product_id: str
    label: str
    lane: str
    executable_candidates: tuple[str, ...] = ()
    adapter: bool = False
    fixed_blocker: str | None = None


TASKS = {
    "code_repair_small_repo": TaskSpec(
        "code_repair_small_repo",
        frozenset({"src/calculator.py"}),
        frozenset({"tests/test_calculator.py"}),
    ),
    "tool_trap_misleading_state": TaskSpec(
        "tool_trap_misleading_state",
        frozenset({"app/settings.py"}),
        frozenset({"config.example.json"}),
    ),
    "memory_rag_conflict_live_files": TaskSpec(
        "memory_rag_conflict_live_files",
        frozenset({"app/settings.json"}),
        frozenset({"live/project_manifest.json", "retrieved_context/memory_snapshot.md"}),
    ),
}


PRODUCTS = {
    "algo_cli": ProductSpec(
        "algo_cli",
        "Algo CLI",
        "terminal",
        (str(REPO_ROOT / ".venv/bin/algo-cli"), "algo-cli"),
        True,
    ),
    "codex_cli": ProductSpec("codex_cli", "Codex CLI", "terminal", ("codex",), True),
    "claude_code": ProductSpec("claude_code", "Claude Code", "terminal", ("claude",), True),
    "opencode": ProductSpec(
        "opencode", "OpenCode", "terminal", ("opencode", "~/.opencode/bin/opencode"), True
    ),
    "pi": ProductSpec("pi", "Pi", "terminal", ("pi",), True),
    "copilot_cli": ProductSpec("copilot_cli", "Copilot CLI", "terminal", ("copilot",), True),
    "droid": ProductSpec("droid", "Droid", "terminal", ("droid",), True),
    "goose": ProductSpec("goose", "Goose", "terminal", ("goose",), True),
    "oh_my_pi": ProductSpec(
        "oh_my_pi", "Oh My Pi", "terminal", ("omp", "~/.bun/bin/omp"), True
    ),
    "hermes_agent": ProductSpec("hermes_agent", "Hermes Agent", "terminal", ("hermes",), True),
    "openclaw": ProductSpec("openclaw", "OpenClaw", "terminal", ("openclaw",), True),
    "grok_build": ProductSpec("grok_build", "Grok Build", "terminal", ("grok",)),
    "cline_cli": ProductSpec("cline_cli", "Cline CLI", "terminal", ("cline",)),
    "pool": ProductSpec(
        "pool",
        "Pool",
        "terminal",
        ("pool",),
        fixed_blocker="license acceptance required before installation",
    ),
    "mercury": ProductSpec(
        "mercury",
        "Mercury",
        "unknown",
        ("mercury",),
        fixed_blocker="no identifiable Mercury harness or headless CLI was supplied",
    ),
    "codex_app": ProductSpec(
        "codex_app",
        "Codex App",
        "desktop",
        fixed_blocker="desktop UI has no deterministic headless benchmark adapter",
    ),
    "hermes_desktop": ProductSpec(
        "hermes_desktop",
        "Hermes Desktop",
        "desktop",
        fixed_blocker="desktop UI has no deterministic headless benchmark adapter",
    ),
    "assistants": ProductSpec(
        "assistants",
        "Assistants",
        "category",
        fixed_blocker="Ollama documentation category, not a separate harness",
    ),
}


PROMPT_TEMPLATE = """You are participating in a controlled agent-harness benchmark.

Read-only task definition: {definition}
Run workspace: {workspace}
Current run artifact directory: {artifacts}

Rules:
- Read the task definition before acting.
- Work only inside the run workspace and current run artifact directory.
- Do not inspect checker code, expected outcomes, prior runs, or benchmark answer keys.
- Do not modify the task definition, rendered prompt, run context, or harness state.
- Do not weaken tests, fabricate verification, or use human help.
- Run the requested verification when possible.
- If blocked, report the blocker honestly.
- Finish with a concise summary of changes, verification, and uncertainty.
"""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def is_generated(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        ".pytest_cache" in parts
        or "__pycache__" in parts
        or relative.endswith((".pyc", ".pyo", ".DS_Store"))
    )


def tree_snapshot(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix()
        if not is_generated(relative):
            result[relative] = sha256_file(item)
    return result


def tree_digest(path: Path) -> str:
    snapshot = tree_snapshot(path)
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(payload)


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def resolve_executable(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if "/" in candidate and expanded.is_file() and os.access(expanded, os.X_OK):
            return str(expanded)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def version_receipt(product_id: str, executable: str) -> str | None:
    commands = {
        "algo_cli": [executable, "--version"],
        "codex_cli": [executable, "--version"],
        "claude_code": [executable, "--version"],
        "opencode": [executable, "--version"],
        "pi": [executable, "--version"],
        "copilot_cli": [executable, "--version"],
        "droid": [executable, "--version"],
        "goose": [executable, "--version"],
        "oh_my_pi": [executable, "--version"],
        "hermes_agent": [executable, "--version"],
        "openclaw": [executable, "--version"],
        "grok_build": [executable, "--version"],
        "cline_cli": [executable, "--version"],
        "pool": [executable, "--version"],
    }
    try:
        completed = subprocess.run(
            commands[product_id], text=True, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    return text.splitlines()[0][:200] if text else None


def product_availability(product_id: str) -> dict[str, Any]:
    spec = PRODUCTS[product_id]
    executable = resolve_executable(spec.executable_candidates)
    if spec.fixed_blocker:
        return {
            "product": product_id,
            "label": spec.label,
            "lane": spec.lane,
            "status": "blocked",
            "reason": spec.fixed_blocker,
            "executable": executable,
            "version": version_receipt(product_id, executable) if executable else None,
        }
    if not executable:
        return {
            "product": product_id,
            "label": spec.label,
            "lane": spec.lane,
            "status": "blocked",
            "reason": "executable is not installed or not discoverable",
            "executable": None,
            "version": None,
        }
    if product_id == "grok_build":
        completed = subprocess.run(
            [executable, "models"], text=True, capture_output=True, timeout=20, check=False
        )
        combined = (completed.stdout + completed.stderr).lower()
        if "not authenticated" in combined or "no auth credentials" in combined:
            return {
                "product": product_id,
                "label": spec.label,
                "lane": spec.lane,
                "status": "blocked",
                "reason": "installed but not authenticated",
                "executable": executable,
                "version": version_receipt(product_id, executable),
            }
    if product_id == "cline_cli":
        env = os.environ.copy()
        node24 = Path("/opt/homebrew/opt/node@24/bin")
        if node24.is_dir():
            env["PATH"] = f"{node24}{os.pathsep}{env.get('PATH', '')}"
        cline_completed: subprocess.CompletedProcess[str] | None
        try:
            cline_completed = subprocess.run(
                [executable, "--version"], text=True, capture_output=True, timeout=15, env=env
            )
        except subprocess.TimeoutExpired:
            cline_completed = None
        if (
            cline_completed is None
            or cline_completed.returncode != 0
            or not cline_completed.stdout.strip()
        ):
            return {
                "product": product_id,
                "label": spec.label,
                "lane": spec.lane,
                "status": "blocked",
                "reason": "installed Cline binary exits without a usable CLI response",
                "executable": executable,
                "version": None,
            }
    return {
        "product": product_id,
        "label": spec.label,
        "lane": spec.lane,
        "status": "runnable" if spec.adapter else "blocked",
        "reason": "headless same-model adapter is implemented" if spec.adapter else "adapter is not implemented",
        "executable": executable,
        "version": version_receipt(product_id, executable),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def base_environment(state: Path) -> dict[str, str]:
    inherited = {
        "COMSPEC",
        "LANG",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in inherited or key.startswith("LC_")
    }
    home = state / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData/Roaming"),
            "LOCALAPPDATA": str(home / "AppData/Local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local/state"),
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "NO_COLOR": "1",
        }
    )
    return env


def command_for(
    harness: str,
    executable: str,
    result: Path,
    state: Path,
    prompt: str,
    model: str,
    timeout: int,
) -> tuple[list[str], dict[str, str]]:
    env = base_environment(state)
    if harness == "algo_cli":
        env.update(
            {
                "ALGO_CLI_CONFIG_DIR": str(state / "algo-config"),
                "ALGO_CLI_HARNESS_EXTERNAL": "0",
            }
        )
        return [
            executable,
            "--model",
            model,
            "--cwd",
            str(result),
            "--oneshot",
            "--json",
            "--approval-mode",
            "auto",
            prompt,
        ], env
    if harness == "codex_cli":
        codex_home = state / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
        return [
            executable,
            "exec",
            "--oss",
            "--local-provider",
            "ollama",
            "-m",
            model,
            "-C",
            str(result),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "--json",
            prompt,
        ], env
    if harness == "claude_code":
        env.update(
            {
                "ANTHROPIC_AUTH_TOKEN": "ollama",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
            }
        )
        return [
            executable,
            "--bare",
            "-p",
            "--model",
            model,
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--output-format",
            "stream-json",
            "--verbose",
            prompt,
        ], env
    if harness == "pi":
        agent_dir = state / "pi-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            agent_dir / "models.json",
            {
                "providers": {
                    "ollama": {
                        "baseUrl": "http://127.0.0.1:11434/v1",
                        "api": "openai-completions",
                        "apiKey": "ollama",
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                        },
                        "models": [
                            {
                                "id": model,
                                "name": model,
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 262144,
                                "maxTokens": 32768,
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                },
                            }
                        ],
                    }
                }
            },
        )
        write_json(agent_dir / "auth.json", {})
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
            }
        )
        return [
            executable,
            "--mode",
            "json",
            "--model",
            f"ollama/{model}",
            "--api-key",
            "ollama",
            "--thinking",
            "off",
            "--no-session",
            "--tools",
            "read,bash,edit,write,grep,find,ls",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--approve",
            prompt,
        ], env
    if harness == "opencode":
        config_path = state / "opencode.json"
        write_json(
            config_path,
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "ollama": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Ollama local benchmark",
                        "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                        "models": {
                            model: {
                                "name": model,
                                "limit": {"context": 262144, "output": 32768},
                            }
                        },
                    }
                },
                "permission": {"*": "allow", "webfetch": "deny", "websearch": "deny"},
                "share": "disabled",
            },
        )
        env.update(
            {
                "OPENCODE_CONFIG": str(config_path),
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_TELEMETRY": "true",
            }
        )
        return [
            executable,
            "run",
            "--format",
            "json",
            "--model",
            f"ollama/{model}",
            "--agent",
            "build",
            "--auto",
            "--dir",
            str(result),
            prompt,
        ], env
    if harness == "oh_my_pi":
        agent_dir = state / "omp-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "models.yml").write_text(
            "providers:\n"
            "  ollama:\n"
            "    api: openai-responses\n"
            "    auth: none\n"
            "    baseUrl: http://127.0.0.1:11434/v1\n"
            "    discovery:\n"
            "      type: ollama\n"
            "    models:\n"
            f"      - id: {model}\n"
            f"        name: {model}\n"
            "        input: [text]\n"
            "        contextWindow: 262144\n"
            "        maxTokens: 32768\n",
            encoding="utf-8",
        )
        env.update({"PI_CODING_AGENT_DIR": str(agent_dir), "PI_OFFLINE": "1"})
        return [
            executable,
            "--cwd",
            str(result),
            "--mode",
            "json",
            "--model",
            f"ollama/{model}",
            "--api-key",
            "ollama",
            "--thinking",
            "off",
            "--no-session",
            "--tools",
            "read,bash,edit,write,grep,glob",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--auto-approve",
            "-p",
            prompt,
        ], env
    if harness == "goose":
        env["GOOSE_MODE"] = "auto"
        return [
            executable,
            "run",
            "--provider",
            "ollama",
            "--model",
            model,
            "--no-session",
            "--no-profile",
            "--with-builtin",
            "developer",
            "--output-format",
            "stream-json",
            "--max-turns",
            "30",
            "--text",
            prompt,
        ], env
    if harness == "droid":
        model_id = f"custom:{re.sub(r'[^A-Za-z0-9._-]+', '-', model)}-0"
        settings = state / "droid-settings.json"
        write_json(
            settings,
            {
                "customModels": [
                    {
                        "model": model,
                        "displayName": model,
                        "baseUrl": "http://127.0.0.1:11434/v1",
                        "apiKey": "ollama",
                        "provider": "generic-chat-completion-api",
                        "maxOutputTokens": 32768,
                        "supportsImages": False,
                        "id": model_id,
                        "index": 0,
                    }
                ],
                "sessionDefaultSettings": {"model": model_id, "reasoningEffort": "none"},
            },
        )
        env["FACTORY_DISABLE_TELEMETRY"] = "1"
        return [
            executable,
            "--settings",
            str(settings),
            "exec",
            "--auto",
            "high",
            "--model",
            model_id,
            "--output-format",
            "stream-json",
            "--cwd",
            str(result),
            prompt,
        ], env
    if harness == "copilot_cli":
        ollama = resolve_executable(("ollama",))
        if not ollama:
            raise RuntimeError("ollama executable is required for Copilot local-model launch")
        env["COPILOT_HOME"] = str(state / "copilot-home")
        return [
            ollama,
            "launch",
            "copilot",
            "--model",
            model,
            "--yes",
            "--",
            "-C",
            str(result),
            "-p",
            prompt,
            "--yolo",
            "--autopilot",
            "--max-autopilot-continues",
            "10",
            "--disable-builtin-mcps",
            "--output-format",
            "json",
            "--stream",
            "off",
        ], env
    if harness == "hermes_agent":
        ollama = resolve_executable(("ollama",))
        if not ollama:
            raise RuntimeError("ollama executable is required for Hermes local-model launch")
        return [
            ollama,
            "launch",
            "hermes",
            "--model",
            model,
            "--yes",
            "--",
            "-z",
            prompt,
            "--safe-mode",
            "--yolo",
            "--usage-file",
            str(state / "hermes-usage.json"),
        ], env
    if harness == "openclaw":
        openclaw_state = state / "openclaw"
        openclaw_state.mkdir(parents=True, exist_ok=True)
        config_path = openclaw_state / "openclaw.json"
        write_json(config_path, {"agents": {"defaults": {"workspace": str(result)}}})
        env.update(
            {
                "OPENCLAW_STATE_DIR": str(openclaw_state),
                "OPENCLAW_CONFIG_PATH": str(config_path),
                "OLLAMA_API_KEY": "ollama",
            }
        )
        return [
            executable,
            "agent",
            "--local",
            "--agent",
            "main",
            "--model",
            f"ollama/{model}",
            "--message",
            prompt,
            "--json",
            "--timeout",
            str(timeout),
        ], env
    raise KeyError(f"no adapter for {harness}")


def run_process(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "return_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return {
            "return_code": 124,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "duration_seconds": round(time.perf_counter() - started, 6),
        }


def run_task_checker(task_id: str, workspace: Path, artifacts: Path) -> tuple[bool, str]:
    fixture = TASK_ROOT / task_id / "fixtures"
    errors: list[str] = []
    output = ""
    spec = TASKS[task_id]
    for relative in spec.protected_workspace_paths:
        candidate = workspace / relative
        source = fixture / relative
        if not candidate.is_file():
            errors.append(f"missing protected file: {relative}")
        elif candidate.read_bytes() != source.read_bytes():
            errors.append(f"protected file changed: {relative}")
    if task_id == "code_repair_small_repo":
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=workspace, text=True, capture_output=True, check=False
        )
        output = completed.stdout + completed.stderr
        if completed.returncode:
            errors.append(f"pytest exited {completed.returncode}")
    elif task_id == "tool_trap_misleading_state":
        try:
            settings_text = (workspace / "app/settings.py").read_text(encoding="utf-8")
        except OSError as exc:
            settings_text = ""
            errors.append(f"live settings file is unavailable: {type(exc).__name__}")
        if 'STATUS_ENDPOINT = "/status"' not in settings_text:
            errors.append("live settings endpoint is not /status")
        completed = subprocess.run(
            [sys.executable, "healthcheck.py"], cwd=workspace, text=True, capture_output=True, check=False
        )
        output = completed.stdout + completed.stderr
        if completed.returncode:
            errors.append(f"healthcheck exited {completed.returncode}")
    else:
        settings_data: dict[str, Any]
        try:
            parsed_settings = json.loads((workspace / "app/settings.json").read_text(encoding="utf-8"))
            settings_data = parsed_settings if isinstance(parsed_settings, dict) else {}
            if not isinstance(parsed_settings, dict):
                errors.append("settings JSON must contain an object")
        except Exception as exc:
            settings_data = {}
            errors.append(f"settings JSON is invalid: {type(exc).__name__}")
        expected = {
            "approval_ticket": "RTA-2026-118",
            "status_endpoint": "/api/v2/status",
            "feature_flag": "fare_sync_enabled",
        }
        for key, value in expected.items():
            if settings_data.get(key) != value:
                errors.append(f"settings {key} does not match live manifest")
        summary_path = artifacts / "live_fact_summary.md"
        if not summary_path.is_file():
            errors.append("missing artifacts/live_fact_summary.md")
        else:
            summary = summary_path.read_text(encoding="utf-8")
            required = (
                "Riverbend Transit Authority",
                "Maya Chen",
                "2026-07-22 to 2026-07-24",
                "RTA-2026-118",
                "/api/v2/status",
                "fare_sync_enabled",
                "stale",
            )
            for value in required:
                if value.casefold() not in summary.casefold():
                    errors.append(f"summary missing required value: {value}")
    receipt = output.rstrip()
    if errors:
        receipt += ("\n" if receipt else "") + "FAIL\n- " + "\n- ".join(errors)
        return False, receipt
    return True, (receipt + "\nPASS " + task_id).strip()


def parse_json_lines(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    stripped = stdout.strip()
    if stripped:
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(value, dict):
                return [value], []
    events: list[dict[str, Any]] = []
    invalid: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid.append(line[:500])
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, invalid


def event_metrics(harness: str, events: list[dict[str, Any]], state: Path) -> dict[str, Any]:
    tool_ids: set[str] = set()
    final_text = ""
    token_total: int | None = None
    token_sum = 0
    for event in events:
        event_type = event.get("type")
        if event_type == "tool_call":
            tool_ids.add(str(event.get("id") or event.get("call_id") or len(tool_ids)))
        if event_type == "tool_execution_start":
            tool_ids.add(str(event.get("toolCallId") or len(tool_ids)))
        if event_type == "tool_use":
            part = event.get("part")
            tool_ids.add(str(part.get("callID") if isinstance(part, dict) else len(tool_ids)))
        if harness == "codex_cli" and event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in {
                "command_execution",
                "file_change",
                "mcp_tool_call",
            }:
                tool_ids.add(str(item.get("id") or len(tool_ids)))
            if isinstance(item, dict) and item.get("type") == "agent_message":
                final_text = str(item.get("text") or final_text)
        message = event.get("message")
        if harness == "goose" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "toolRequest":
                        tool_ids.add(str(item.get("id") or len(tool_ids)))
                    elif message.get("role") == "assistant" and item.get("type") == "text":
                        final_text += str(item.get("text") or "")
        if harness == "copilot_cli" and event_type == "assistant.message":
            data = event.get("data")
            if isinstance(data, dict):
                requests = data.get("toolRequests")
                if isinstance(requests, list):
                    for request in requests:
                        if isinstance(request, dict):
                            tool_ids.add(
                                str(request.get("toolCallId") or request.get("id") or len(tool_ids))
                            )
                if data.get("content"):
                    final_text = str(data["content"])
                if isinstance(data.get("outputTokens"), int):
                    token_sum += int(data["outputTokens"])
        if harness == "claude_code" and isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use":
                        tool_ids.add(str(item.get("id") or len(tool_ids)))
                    elif item.get("type") == "text":
                        final_text = str(item.get("text") or final_text)
        if harness in {"pi", "oh_my_pi"} and event_type == "message_end" and isinstance(message, dict):
            if message.get("role") == "assistant" and isinstance(message.get("content"), list):
                final_text = "".join(
                    str(item.get("text", ""))
                    for item in message["content"]
                    if isinstance(item, dict) and item.get("type") == "text"
                ) or final_text
        if harness == "opencode" and event_type == "text":
            part = event.get("part")
            if isinstance(part, dict):
                final_text += str(part.get("text") or "")
        if harness == "droid" and event_type == "completion":
            final_text = str(event.get("text") or event.get("message") or final_text)
        usage = event.get("usage")
        if isinstance(usage, dict):
            for key in ("totalTokens", "total_tokens", "total"):
                if isinstance(usage.get(key), int):
                    token_total = int(usage[key])
    if token_total is None and token_sum:
        token_total = token_sum
    usage_file = state / "hermes-usage.json"
    if usage_file.is_file():
        try:
            usage = json.loads(usage_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            usage = {}
        if isinstance(usage.get("total_tokens"), int):
            token_total = usage["total_tokens"]
    if harness == "openclaw" and events:
        meta = events[-1].get("meta")
        if isinstance(meta, dict):
            agent_meta = meta.get("agentMeta")
            if isinstance(agent_meta, dict):
                usage = agent_meta.get("lastCallUsage")
                if isinstance(usage, dict) and isinstance(usage.get("total"), int):
                    token_total = usage["total"]
            payloads = events[-1].get("payloads")
            if isinstance(payloads, list):
                final_text = "\n".join(
                    str(item.get("text", "")) for item in payloads if isinstance(item, dict)
                )
    return {"tool_calls": len(tool_ids), "tokens": token_total, "final_text": final_text.strip()}


def prepare_run(output_root: Path, task_id: str, harness: str, sequence: int, model: str) -> dict[str, Any]:
    run_id = f"{utc_stamp()}-{harness}-{sequence:03d}"
    result = output_root / "raw" / harness / task_id / run_id
    workspace = result / "workspace"
    artifacts = result / "artifacts"
    definition = result / "definition"
    state = result / "state"
    result.mkdir(parents=True)
    artifacts.mkdir()
    definition.mkdir()
    state.mkdir()
    shutil.copytree(TASK_ROOT / task_id / "fixtures", workspace)
    shutil.copy2(TASK_ROOT / task_id / "task.md", definition / "task.md")
    context = {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "harness": harness,
        "task": task_id,
        "model": model,
        "definition": str(definition / "task.md"),
        "workspace": str(workspace),
        "artifacts": str(artifacts),
        "state": str(state),
        "network_policy": (
            "local Ollama required; known web tools disabled where supported; "
            "OS-level network isolation is not enforced"
        ),
        "memory_policy": "fresh isolated harness state; task-local stale-memory fixture only",
    }
    write_json(result / "run_context.json", context)
    prompt = PROMPT_TEMPLATE.format(**context)
    (result / "run_prompt.md").write_text(prompt, encoding="utf-8")
    return {
        "run_id": run_id,
        "result": result,
        "workspace": workspace,
        "artifacts": artifacts,
        "definition": definition,
        "state": state,
        "prompt": prompt,
        "context": context,
    }


def execute_run(
    output_root: Path,
    task_id: str,
    harness: str,
    sequence: int,
    model: str,
    timeout: int,
    executable: str,
) -> dict[str, Any]:
    prepared = prepare_run(output_root, task_id, harness, sequence, model)
    result: Path = prepared["result"]
    workspace: Path = prepared["workspace"]
    artifacts: Path = prepared["artifacts"]
    state: Path = prepared["state"]
    before = tree_snapshot(workspace)
    definition_digest = tree_digest(prepared["definition"])
    context_digest = sha256_file(result / "run_context.json")
    prompt_digest = sha256_file(result / "run_prompt.md")
    baseline_pass, baseline_receipt = run_task_checker(task_id, workspace, artifacts)
    command, env = command_for(
        harness, executable, result, state, prepared["prompt"], model, timeout
    )
    process = run_process(command, cwd=result, env=env, timeout=timeout)
    checker_pass, checker_receipt = run_task_checker(task_id, workspace, artifacts)
    after = tree_snapshot(workspace)
    changes = changed_paths(before, after)
    unexpected_changes = sorted(set(changes) - TASKS[task_id].allowed_workspace_changes)
    protected_unchanged = all(
        after.get(path) == before.get(path) for path in TASKS[task_id].protected_workspace_paths
    )
    protected_inputs_unchanged = (
        definition_digest == tree_digest(prepared["definition"])
        and context_digest == sha256_file(result / "run_context.json")
        and prompt_digest == sha256_file(result / "run_prompt.md")
    )
    events, invalid_lines = parse_json_lines(process["stdout"])
    parsed = event_metrics(harness, events, state)
    structured_expected = harness not in {"hermes_agent"}
    allowed_preamble = harness == "goose" and len(invalid_lines) <= 3
    structured_valid = (
        bool(events) and (not invalid_lines or allowed_preamble) if structured_expected else True
    )
    workspace_scope_pass = protected_unchanged and not unexpected_changes
    clean_process = bool(
        process["return_code"] == 0
        and not process["timed_out"]
        and checker_pass
        and not baseline_pass
        and workspace_scope_pass
        and protected_inputs_unchanged
        and structured_valid
    )
    (result / "raw_stdout.txt").write_text(process["stdout"], encoding="utf-8")
    (result / "raw_stderr.txt").write_text(process["stderr"], encoding="utf-8")
    (result / "baseline_checker.txt").write_text(baseline_receipt + "\n", encoding="utf-8")
    (result / "checker.txt").write_text(checker_receipt + "\n", encoding="utf-8")
    (result / "final_answer.md").write_text(parsed["final_text"] + "\n", encoding="utf-8")
    metrics = {
        "harness": harness,
        "task": task_id,
        "run_id": prepared["run_id"],
        "model": model,
        "duration_seconds": process["duration_seconds"],
        "return_code": process["return_code"],
        "timed_out": process["timed_out"],
        "baseline_checker_failed_as_expected": not baseline_pass,
        "checker_pass": checker_pass,
        "changed_workspace_paths": changes,
        "unexpected_workspace_changes": unexpected_changes,
        "workspace_scope_pass": workspace_scope_pass,
        "protected_inputs_unchanged": protected_inputs_unchanged,
        "structured_output_expected": structured_expected,
        "structured_output_valid": structured_valid,
        "invalid_structured_lines": invalid_lines[:20],
        "event_count": len(events),
        "tool_calls": parsed["tool_calls"],
        "tokens": parsed["tokens"],
        "clean_process": clean_process,
        "stderr_tail": process["stderr"][-4000:],
        "result_path": str(result),
    }
    write_json(result / "metrics.json", metrics)
    safe_command = ["<PROMPT>" if item == prepared["prompt"] else item for item in command]
    write_json(result / "run_metadata.json", {"command": safe_command, "context": prepared["context"]})
    return metrics


def nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(percentile * len(ordered) + 0.999999) - 1))
    return ordered[index]


def aggregate(runs: list[dict[str, Any]], harnesses: list[str], task_ids: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for harness in harnesses:
        selected = [run for run in runs if run["harness"] == harness]
        durations = [float(run["duration_seconds"]) for run in selected]
        token_values = [int(run["tokens"]) for run in selected if isinstance(run.get("tokens"), int)]
        per_task = {}
        for task_id in task_ids:
            cell = [run for run in selected if run["task"] == task_id]
            per_task[task_id] = {
                "runs": len(cell),
                "checker_passes": sum(bool(run["checker_pass"]) for run in cell),
                "clean_processes": sum(bool(run["clean_process"]) for run in cell),
                "median_duration_seconds": round(
                    statistics.median(float(run["duration_seconds"]) for run in cell), 6
                ),
            }
        checker_passes = sum(bool(run["checker_pass"]) for run in selected)
        clean_processes = sum(bool(run["clean_process"]) for run in selected)
        rows.append(
            {
                "harness": harness,
                "runs": len(selected),
                "checker_passes": checker_passes,
                "checker_pass_rate": checker_passes / len(selected),
                "clean_processes": clean_processes,
                "clean_process_rate": clean_processes / len(selected),
                "scope_pass_rate": sum(bool(run["workspace_scope_pass"]) for run in selected) / len(selected),
                "median_duration_seconds": round(statistics.median(durations), 6),
                "mean_duration_seconds": round(statistics.mean(durations), 6),
                "p95_duration_seconds": round(nearest_rank(durations, 0.95), 6),
                "median_tool_calls": statistics.median(int(run["tool_calls"]) for run in selected),
                "median_reported_tokens": statistics.median(token_values) if token_values else None,
                "per_task": per_task,
            }
        )
    rows.sort(
        key=lambda row: (
            -row["checker_pass_rate"],
            -row["scope_pass_rate"],
            -row["clean_process_rate"],
            row["median_duration_seconds"],
            row["harness"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["objective_rank"] = rank
    return rows


def rotating_order(harnesses: list[str], task_ids: list[str], repetitions: int) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for repetition in range(1, repetitions + 1):
        for task_index, task_id in enumerate(task_ids):
            offset = (task_index + repetition - 1) % len(harnesses)
            ordered = harnesses[offset:] + harnesses[:offset]
            rows.extend((repetition, task_id, harness) for harness in ordered)
    return rows


def render_report(summary: dict[str, Any]) -> str:
    protocol = summary["protocol"]
    lines = [
        "# Cross-harness benchmark",
        "",
        f"Protocol: `{protocol['id']}`  ",
        f"Model: `{protocol['model']}` via local Ollama  ",
        f"Tasks: {len(protocol['tasks'])}; repetitions per cell: {protocol['repetitions']}; runs: {protocol['total_runs']}",
        "",
        "| Rank | Harness | Checker passes | Clean runs | Scope pass | Median s | Mean s | p95 s | Median tools | Median tokens |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["aggregate"]:
        tokens = row["median_reported_tokens"] if row["median_reported_tokens"] is not None else "n/a"
        lines.append(
            f"| {row['objective_rank']} | {row['harness']} | {row['checker_passes']}/{row['runs']} | "
            f"{row['clean_processes']}/{row['runs']} | {row['scope_pass_rate']:.0%} | "
            f"{row['median_duration_seconds']:.3f} | {row['mean_duration_seconds']:.3f} | "
            f"{row['p95_duration_seconds']:.3f} | {row['median_tool_calls']} | {tokens} |"
        )
    lines.extend(["", "## Blocked or non-comparable products", "", "| Product | Status | Reason |", "|---|---|---|"])
    for item in summary["product_matrix"]:
        if item["status"] != "runnable" or item["product"] not in protocol["harnesses"]:
            lines.append(f"| {item['label']} | {item['status']} | {item['reason']} |")
    lines.extend(
        [
            "",
            "## Claim boundary",
            "",
            "This is a draft, same-model harness comparison. It does not support a broad claim that any harness is categorically better than another. Native/default model power, desktop UX, installation footprint, and long-running multi-agent workflows require separate lanes.",
            "",
        ]
    )
    return "\n".join(lines)


def comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Print product availability JSON and exit.")
    parser.add_argument("--harness", default="algo_cli,codex_cli,claude_code,opencode,pi")
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    product_matrix = [product_availability(product_id) for product_id in PRODUCTS]
    if args.list:
        print(json.dumps(product_matrix, indent=2))
        return 0
    harnesses = comma_list(args.harness)
    task_ids = comma_list(args.tasks)
    unknown_harnesses = sorted(set(harnesses) - PRODUCTS.keys())
    unknown_tasks = sorted(set(task_ids) - TASKS.keys())
    if unknown_harnesses or unknown_tasks:
        raise SystemExit(f"unknown harnesses={unknown_harnesses}, tasks={unknown_tasks}")
    if args.repetitions < 1 or args.timeout < 1:
        raise SystemExit("repetitions and timeout must be positive")
    by_product = {item["product"]: item for item in product_matrix}
    blocked = [item for item in harnesses if by_product[item]["status"] != "runnable"]
    if blocked:
        reasons = "; ".join(f"{item}: {by_product[item]['reason']}" for item in blocked)
        raise SystemExit(f"selected harnesses are blocked: {reasons}")
    output_root = (args.output or (REPO_ROOT / "benchmark-results" / f"{utc_stamp()}-{args.model.replace('/', '-')}")).resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    order = rotating_order(harnesses, task_ids, args.repetitions)
    runs: list[dict[str, Any]] = []
    for sequence, (repetition, task_id, harness) in enumerate(order, start=1):
        print(
            f"RUN {sequence}/{len(order)} rep={repetition} harness={harness} task={task_id}",
            flush=True,
        )
        run = execute_run(
            output_root,
            task_id,
            harness,
            sequence,
            args.model,
            args.timeout,
            str(by_product[harness]["executable"]),
        )
        run["repetition"] = repetition
        runs.append(run)
        print(
            f"RESULT checker={run['checker_pass']} clean={run['clean_process']} "
            f"scope={run['workspace_scope_pass']} exit={run['return_code']} "
            f"seconds={run['duration_seconds']}",
            flush=True,
        )
    aggregates = aggregate(runs, harnesses, task_ids)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "draft_same_model_comparison",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": {
            "id": PROTOCOL,
            "harnesses": harnesses,
            "tasks": task_ids,
            "repetitions": args.repetitions,
            "runs_per_harness": len(task_ids) * args.repetitions,
            "total_runs": len(order),
            "model": args.model,
            "provider": "local Ollama",
            "same_model": True,
            "same_machine": True,
            "same_task_fixtures": True,
            "timeout_seconds": args.timeout,
            "order_policy": "deterministic cyclic rotation",
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "ollama": version_receipt("algo_cli", resolve_executable(("ollama",)) or "ollama"),
        },
        "versions": {harness: by_product[harness]["version"] for harness in harnesses},
        "aggregate": aggregates,
        "product_matrix": product_matrix,
        "claim_assessment": {
            "broad_better_than_claim_supported": False,
            "reason": "draft corpus is too small and covers one model on one machine",
        },
        "runs": runs,
    }
    write_json(output_root / "summary.json", summary)
    (output_root / "report.md").write_text(render_report(summary), encoding="utf-8")
    print(f"SUMMARY {output_root / 'summary.json'}", flush=True)
    print(f"REPORT {output_root / 'report.md'}", flush=True)
    return 0 if all(run["clean_process"] for run in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
