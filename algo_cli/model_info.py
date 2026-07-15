"""Ollama model metadata cache.

Loads metadata via ``ollama show MODEL`` (preferred when the model is installed)
and/or ``client.show()``, persists to CONFIG_DIR/model_info/, and writes a
markdown harness record to CONFIG_DIR/models/ for harness RAG.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text
from .model_aliases import normalize_codex_model


MODEL_INFO_DIR = CONFIG_DIR / "model_info"
MODELS_DIR = CONFIG_DIR / "models"

_THINKING_FAMILIES: frozenset[str] = frozenset({"qwen3", "qwen3moe"})
_VISION_FAMILIES: frozenset[str] = frozenset({"clip", "llava", "minicpm-v"})
_SIZE_RE = re.compile(r"([\d.]+)\s*([BbMmTtKk])", re.ASCII)
# Gemini models routed via Ollama Cloud require thought_signature round-tripping
# which the current Ollama Python SDK does not expose. Match these by name so the
# agent loop can disable thinking before sending tool-call sequences.
_GEMINI_NAME_RE = re.compile(r"^gemini[-_]", re.IGNORECASE)
# Grok models route through xAI's documented API-key auth and the
# OpenAI-compatible chat surface — see xai_client.py.
_GROK_NAME_RE = re.compile(r"^grok[-_]", re.IGNORECASE)
# ChatGPT/OpenAI models routed through ChatGPT OAuth. Do not catch gpt-oss,
# which is an Ollama/Ollama Cloud model family.
_CHATGPT_NAME_RE = re.compile(r"^(?:chatgpt[-_]|gpt-(?!oss)|o[134](?:[-_]|$))", re.IGNORECASE)

_CACHE: dict[str, dict[str, Any]] = {}


def _safe_name(model: str) -> str:
    return re.sub(r"[^\w\-.]", "_", model)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if hasattr(obj, key):
        return getattr(obj, key, default)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _context_length(raw_info: dict) -> int | None:
    for key, val in raw_info.items():
        if "context_length" in str(key).lower() and isinstance(val, int) and val > 0:
            return val
    return None


def _bare_model_name(model: str) -> str:
    return normalize_codex_model(model).split(":", 1)[0].strip().lower()


_SHOW_CTX_RE = re.compile(r"^\s*context\s+length\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
_SHOW_ARCH_RE = re.compile(r"^\s*architecture\s+(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_SHOW_PARAMS_RE = re.compile(r"^\s*parameters\s+(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_SHOW_QUANT_RE = re.compile(r"^\s*quantization\s+(\S*)\s*$", re.IGNORECASE | re.MULTILINE)
_VALID_QUANT_RE = re.compile(r"^(?=.*[A-Z])(?=.*\d)[A-Z0-9_]+$", re.ASCII)
_KNOWN_CAPABILITIES = {"completion", "tools", "tool", "thinking", "vision", "embedding", "insert"}

# Static fallbacks only when ``ollama show`` and client.show() both fail.
_CLOUD_MODEL_HINTS: dict[str, dict[str, Any]] = {
    "minimax-m3": {"context_length": 524_288, "supports_thinking": True},
    "glm-5.2": {"context_length": 1_000_000, "supports_thinking": True},
    "glm-5.1": {"context_length": 202_752},
    "glm-5": {"context_length": 202_752},
    "qwen3-coder": {"context_length": 262_144},
    "qwen3": {"context_length": 262_144},
    "deepseek-v3.1": {"context_length": 131_072},
}


def _show_name_candidates(model: str, *, cloud: bool = False) -> list[str]:
    """Names to try with ``ollama show`` (config may omit ``:cloud`` tag)."""
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        cleaned = name.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            names.append(cleaned)

    add(model)
    if cloud and ":" not in model:
        add(f"{model}:cloud")
    return names


def parse_ollama_show_output(text: str, *, model: str = "") -> dict[str, Any]:
    """Parse human-readable output from ``ollama show MODEL``."""
    capabilities: set[str] = set()
    in_capabilities = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("capabilities"):
            inline = [part.lower() for part in stripped.split()[1:]]
            capabilities.update(part for part in inline if part in _KNOWN_CAPABILITIES)
            in_capabilities = True
            continue
        if in_capabilities:
            if not stripped or stripped.startswith("Metadata"):
                in_capabilities = False
                continue
            token = stripped.split()[0].lower().rstrip(":")
            if token in _KNOWN_CAPABILITIES:
                capabilities.add(token)

    ctx_match = _SHOW_CTX_RE.search(text)
    arch_match = _SHOW_ARCH_RE.search(text)
    params_match = _SHOW_PARAMS_RE.search(text)
    quant_match = _SHOW_QUANT_RE.search(text)

    family = (arch_match.group(1) if arch_match else "").lower()
    families = [family] if family else []
    parameter_size = params_match.group(1) if params_match else ""
    if parameter_size.isdigit() and int(parameter_size) > 0:
        parameter_size = f"{int(parameter_size):,}"
    quantization = (quant_match.group(1) if quant_match else "").strip()
    if quantization and not _VALID_QUANT_RE.match(quantization):
        quantization = ""

    return {
        "name": model,
        "family": family,
        "families": families,
        "parameter_size": parameter_size,
        "quantization": quantization,
        "context_length": int(ctx_match.group(1)) if ctx_match else None,
        "supports_thinking": "thinking" in capabilities or family in _THINKING_FAMILIES,
        "supports_vision": "vision" in capabilities or bool(_VISION_FAMILIES & set(families)),
        "supports_tools": "tools" in capabilities or "tool" in capabilities,
        "capabilities": sorted(capabilities),
    }


def fetch_model_info_from_cli(model: str, *, cloud: bool = False) -> dict[str, Any] | None:
    """Run ``ollama show`` and parse model metadata (works for local and :cloud tags)."""
    ollama = shutil.which("ollama")
    if not ollama:
        return None
    for candidate in _show_name_candidates(model, cloud=cloud):
        try:
            proc = subprocess.run(
                [ollama, "show", candidate],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            continue
        parsed = parse_ollama_show_output(proc.stdout, model=model)
        if not parsed.get("context_length"):
            continue
        parsed["fetched_at"] = time.time()
        parsed["source"] = "ollama-show"
        parsed["show_name"] = candidate
        return parsed
    return None


def _normalize_sdk_show(response: Any, model: str) -> dict[str, Any]:
    details = _get(response, "details")
    raw_info = _get(response, "model_info") or {}
    if not isinstance(raw_info, dict):
        try:
            raw_info = dict(raw_info)
        except Exception:
            raw_info = {}

    family = str(_get(details, "family") or "").lower()
    families_raw = _get(details, "families") or []
    families = [str(f).lower() for f in families_raw]

    return {
        "name": model,
        "family": family,
        "families": families,
        "parameter_size": str(_get(details, "parameter_size") or ""),
        "quantization": str(_get(details, "quantization_level") or ""),
        "context_length": _context_length(raw_info),
        "supports_thinking": family in _THINKING_FAMILIES,
        "supports_vision": bool(_VISION_FAMILIES & set(families)),
        "supports_tools": True,
        "fetched_at": time.time(),
        "source": "ollama-sdk",
    }


def _merge_info_fields(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if key in {"fetched_at", "source", "show_name"}:
            if key not in merged or merged.get(key) in (None, ""):
                merged[key] = value
            continue
        if merged.get(key) in (None, "", 0) and value not in (None, "", 0):
            merged[key] = value
    return merged


def fetch_model_info(client: Any | None, model: str, *, cloud: bool = False) -> dict[str, Any]:
    """Load model metadata from ``ollama show``, SDK show(), or static hints."""
    info: dict[str, Any] | None = None
    if client is not None:
        try:
            response = client.show(model)
            info = _normalize_sdk_show(response, model)
        except Exception as exc:
            info = {"name": model, "error": str(exc), "fetched_at": time.time()}

    cli_info = fetch_model_info_from_cli(model, cloud=cloud)
    if cli_info:
        if info is None or "error" in info:
            info = cli_info
        else:
            info = _merge_info_fields(info, cli_info)

    if info is None:
        hints = cloud_model_hints(model)
        if hints:
            info = {"name": model, **hints, "provider": "ollama-cloud", "source": "hints"}
        else:
            info = {"name": model, "error": "model metadata unavailable", "fetched_at": time.time()}

    return merge_model_hints(info, model)


def load_model_info(model: str) -> dict[str, Any] | None:
    if model in _CACHE:
        return _CACHE[model]
    path = MODEL_INFO_DIR / f"{_safe_name(model)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[model] = data
        return data
    except Exception:
        return None


def save_model_info(model: str, info: dict[str, Any]) -> None:
    MODEL_INFO_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_INFO_DIR / f"{_safe_name(model)}.json"
    try:
        _atomic_write_text(path, json.dumps(info, ensure_ascii=False, indent=2))
        _CACHE[model] = info
    except Exception:
        pass


def ensure_model_info(client: Any, model: str, *, cloud: bool = False) -> dict[str, Any]:
    """Return cached model info, fetching via ollama show / SDK if needed."""
    info = load_model_info(model)
    if info and "error" not in info and get_context_length(info):
        return merge_model_hints(info, model)
    info = fetch_model_info(client, model, cloud=cloud)
    if "error" not in info and get_context_length(info):
        save_model_info(model, info)
        write_model_record(model, info)
    return info


def write_model_record(model: str, info: dict[str, Any]) -> None:
    """Write a markdown harness record for this model to CONFIG_DIR/models/."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(model)
    path = MODELS_DIR / f"{safe}.md"

    family = info.get("family", "")
    params = info.get("parameter_size", "")
    quant = info.get("quantization", "")
    ctx = info.get("context_length")
    thinks = info.get("supports_thinking", False)
    vision = info.get("supports_vision", False)

    description_parts = []
    if params:
        description_parts.append(f"{params} parameters")
    if quant:
        description_parts.append(quant)
    if ctx:
        description_parts.append(f"{ctx:,} context")
    if thinks:
        description_parts.append("supports thinking")
    if vision:
        description_parts.append("supports vision")
    description = ", ".join(description_parts) or "local Ollama model"

    tags = ["model", "ollama"]
    if family:
        tags.append(family)
    if thinks:
        tags.append("thinking")
    if vision:
        tags.append("vision")

    content = (
        f"---\n"
        f"id: ollama:model:{safe}\n"
        f"harness: algo-cli\n"
        f"kind: model\n"
        f"title: {model}\n"
        f"description: {description}\n"
        f"tags: [{', '.join(tags)}]\n"
        f"---\n"
        f"# {model}\n\n"
        f"**Family:** {family or '?'}  \n"
        f"**Parameters:** {params or '?'}  \n"
        f"**Quantization:** {quant or '?'}  \n"
        f"**Context Length:** {ctx or '?'}  \n"
        f"**Supports Thinking:** {'yes' if thinks else 'no'}  \n"
        f"**Supports Vision:** {'yes' if vision else 'no'}  \n"
        f"**Supports Tools:** yes  \n"
    )

    try:
        _atomic_write_text(path, content)
    except Exception:
        pass


def get_context_length(info: dict[str, Any]) -> int | None:
    raw = info.get("context_length")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def cloud_model_hints(model: str) -> dict[str, Any]:
    """Static metadata from Ollama library when show() returns incomplete fields."""
    bare = _bare_model_name(model)
    hinted = _CLOUD_MODEL_HINTS.get(bare)
    return dict(hinted) if hinted else {}


def merge_model_hints(info: dict[str, Any], model: str) -> dict[str, Any]:
    """Fill missing context/thinking fields from cloud hints without overwriting API data."""
    merged = dict(info)
    for key, value in cloud_model_hints(model).items():
        if merged.get(key) in (None, "", 0):
            merged[key] = value
    return merged


def resolve_model_info(cfg: Any, client: Any | None) -> dict[str, Any]:
    """Best-effort model metadata for footer/status (xAI synth, ollama show, SDK, cache)."""
    model = str(getattr(cfg, "model", "") or "")
    if not model:
        return {}
    if is_xai_model(model):
        return synthesize_xai_info(model)
    if is_chatgpt_model(model):
        return synthesize_chatgpt_info(model)

    cloud = bool(getattr(cfg, "cloud", False))
    try:
        info = ensure_model_info(client, model, cloud=cloud) if client is not None else None
        if info and "error" not in info and get_context_length(info):
            return info
    except Exception:
        pass

    cli_info = fetch_model_info_from_cli(model, cloud=cloud)
    if cli_info:
        merged = merge_model_hints(cli_info, model)
        if "error" not in merged:
            save_model_info(model, merged)
        return merged

    cached = load_model_info(model)
    if cached and "error" not in cached:
        return merge_model_hints(cached, model)

    fetched = fetch_model_info(client, model, cloud=cloud)
    if fetched and "error" not in fetched:
        if get_context_length(fetched):
            save_model_info(model, fetched)
        return fetched
    return {"name": model}


def is_cloud_model_name(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered.endswith(":cloud") or lowered.endswith("-cloud") or ":cloud-" in lowered


def effective_context_limits(
    cfg: Any,
    model_info: dict[str, Any] | None = None,
) -> tuple[int, int | None]:
    """Return (runtime_limit, model_native_limit).

    runtime_limit matches the window agent_loop actually requests: the
    model-adaptive profile window when model_adaptive is on (which already
    honors explicit /ctx overrides), else cfg.num_ctx — capped at the model's
    native context when known. Compaction and the footer must use this value,
    not the native window, or long sessions silently truncate before
    compaction ever fires.
    """
    cfg_limit = max(int(getattr(cfg, "num_ctx", 8192) or 8192), 1)
    if getattr(cfg, "model_adaptive", False):
        try:
            from . import model_profile as _model_profile

            cfg_limit = max(int(_model_profile.effective_params(cfg, model_info).num_ctx), 1)
        except Exception:
            pass
    native = get_context_length(model_info or {})
    if native is None:
        return cfg_limit, None
    return min(cfg_limit, native), native


def parameter_size_billions(info: dict[str, Any]) -> float | None:
    """Parse parameter_size string like '8.2B', '70B', '235B', '500M', '1T' → billions.

    Also handles bare raw counts (e.g. '756,162,687,872' from ``ollama show``)
    by treating them as the exact parameter count and dividing by 1e9.

    Returns None when the field is absent or unparseable.
    """
    raw = info.get("parameter_size", "")
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace(",", "")
    m = _SIZE_RE.search(cleaned)
    if m:
        try:
            val = float(m.group(1))
        except ValueError:
            return None
        unit = m.group(2).upper()
        if unit == "T":
            return val * 1000.0
        if unit == "B":
            return val
        if unit == "M":
            return val / 1000.0
        if unit == "K":
            return val / 1_000_000.0
        return val
    # No unit suffix — try parsing as a raw parameter count.
    try:
        return float(cleaned) / 1e9
    except ValueError:
        return None


def supports_thinking(info: dict[str, Any]) -> bool:
    # If we have no model info (cloud mode, show() failed), don't suppress thinking —
    # return True so cfg.show_thinking is respected as-is.
    if "supports_thinking" not in info:
        return True
    return bool(info["supports_thinking"])


def is_gemini_model(model: str) -> bool:
    """Detect Gemini-family models by name (e.g. 'gemini-3-flash-preview:cloud').

    Gemini routed via Ollama Cloud requires preserving thought_signature on every
    tool-call round-trip; the Ollama Python SDK does not currently surface that
    field. Disable thinking for these models to avoid 400 errors on round 2.
    """
    if not model or not isinstance(model, str):
        return False
    bare = model.split(":", 1)[0]
    return bool(_GEMINI_NAME_RE.match(bare))


def is_xai_model(model: str) -> bool:
    """Detect Grok models routed through xAI (e.g. 'grok-4-latest', 'grok-3').

    Grok models use xAI API-key auth against api.x.ai/v1 instead of the local
    Ollama daemon. The agent loop branches on this to swap clients.
    """
    if not model or not isinstance(model, str):
        return False
    bare = model.split(":", 1)[0]
    return bool(_GROK_NAME_RE.match(bare))


def is_chatgpt_model(model: str) -> bool:
    """Detect ChatGPT/OpenAI models routed through ChatGPT OAuth.

    This intentionally excludes gpt-oss so Ollama Cloud/open-weight models are
    not stolen by the OpenAI provider route.
    """
    if not model or not isinstance(model, str):
        return False
    bare = _bare_model_name(model)
    return bool(_CHATGPT_NAME_RE.match(bare))


_CHATGPT_CONTEXT_LENGTHS: dict[str, int] = {
    # The ChatGPT/Codex subscription catalog currently exposes a 272K runtime
    # window for these models. Public API model pages advertise a wider window,
    # but Algo routes this family through the subscription backend.
    "gpt-5.6": 272_000,
    "gpt-5.6-sol": 272_000,
    "gpt-5.6-terra": 272_000,
    "gpt-5.6-luna": 272_000,
    "gpt-5.5": 1_000_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 400_000,
}


def synthesize_chatgpt_info(model: str) -> dict[str, Any]:
    """Build a model_info dict for ChatGPT/OpenAI models (no client.show())."""
    bare = _bare_model_name(model)
    return {
        "name": model,
        "family": "chatgpt",
        "families": ["chatgpt", "openai"],
        "parameter_size": "",
        "quantization": "",
        "context_length": _CHATGPT_CONTEXT_LENGTHS.get(bare, 128_000),
        "supports_thinking": True,
        "supports_vision": bare.startswith("gpt-5.6"),
        "supports_tools": True,
        "provider": "chatgpt",
    }


def synthesize_xai_info(model: str) -> dict[str, Any]:
    """Build a model_info dict for xAI Grok models (no client.show() available)."""
    return {
        "name": model,
        "family": "grok",
        "families": ["grok"],
        "parameter_size": "",
        "quantization": "",
        "context_length": 131072,
        "supports_thinking": True,
        "supports_vision": False,
        "supports_tools": True,
        "provider": "xai",
    }
