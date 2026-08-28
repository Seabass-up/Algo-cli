"""Model/host routing helpers (cloud vs local vs xAI)."""

from __future__ import annotations

import os

from .config import Config, load_runtime_env
from . import model_info as _model_info_module


MODEL_PROVIDERS = frozenset({"auto", "ollama", "xai", "chatgpt"})


def resolved_model_provider(cfg: Config, model: str | None = None) -> str:
    """Resolve a model provider, honoring an explicit choice for the active model."""

    selected_model = str(model if model is not None else cfg.model)
    provider = "auto"
    if model is None or selected_model == cfg.model:
        candidate = str(getattr(cfg, "model_provider", "auto") or "auto").strip().casefold()
        if candidate in MODEL_PROVIDERS:
            provider = candidate
    if provider != "auto":
        return provider
    if _model_info_module.is_xai_model(selected_model):
        return "xai"
    if _model_info_module.is_chatgpt_model(selected_model):
        return "chatgpt"
    return "ollama"


def routes_to_xai(cfg: Config, model: str | None = None) -> bool:
    return resolved_model_provider(cfg, model) == "xai"


def routes_to_chatgpt(cfg: Config, model: str | None = None) -> bool:
    return resolved_model_provider(cfg, model) == "chatgpt"


def is_cloud_model_name(name: str) -> bool:
    return name.endswith(":cloud") or name.endswith("-cloud") or ":cloud-" in name


def runtime_mode_label(cfg: Config) -> str:
    """Return the configured provider route used by status and dashboard UI."""

    if routes_to_xai(cfg):
        return "xai"
    if routes_to_chatgpt(cfg):
        return "chatgpt"
    return "cloud" if cfg.cloud else "local"


def _runtime_ollama_api_key() -> str:
    """Return OLLAMA_API_KEY after loading Algo CLI's runtime env file.

    Tool/API subprocesses may start without inheriting the user's shell
    environment. Loading here keeps routing/status checks consistent with
    tool clients that already call ``load_runtime_env()``.
    """
    load_runtime_env(override=True)
    return os.environ.get("OLLAMA_API_KEY", "").strip()


def uses_ollama_cloud(cfg: Config) -> bool:
    """Whether chat traffic should route through Ollama Cloud's direct API.

    A ``:cloud`` model can also be served by a signed-in local Ollama daemon.
    The first-run picker represents that case as ``cloud via local Ollama`` and
    leaves ``cfg.cloud`` false. Even if stale config has ``cfg.cloud`` true, the
    direct API route is active only when ``OLLAMA_API_KEY`` is present.
    """
    if routes_to_xai(cfg) or routes_to_chatgpt(cfg):
        return False
    return bool(cfg.cloud and _runtime_ollama_api_key())


def effective_runtime_host(cfg: Config) -> str:
    """Provider endpoint label for session_start / ops (not necessarily cfg.host)."""
    if routes_to_xai(cfg):
        return "xai"
    if routes_to_chatgpt(cfg):
        return "chatgpt"
    if uses_ollama_cloud(cfg):
        return "https://ollama.com"
    return cfg.host


def require_cloud_api_key(cfg: Config) -> None:
    """Fail fast before a direct Cloud API chat call when OLLAMA_API_KEY is missing."""
    if not uses_ollama_cloud(cfg):
        return
    if not _runtime_ollama_api_key():
        raise ValueError(
            "OLLAMA_API_KEY is required for direct Ollama Cloud API mode. "
            "Set it in the process environment or in ~/.algo_cli/env."
        )


def is_embedding_model_name(name: str) -> bool:
    lowered = name.lower()
    return (
        "embed" in lowered
        or "embedding" in lowered
        or lowered.startswith("nomic-embed")
        or "minilm" in lowered
        or "paraphrase" in lowered
    )


def is_vision_model_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in ("vision", "-vl", "llava", "qwen2.5-vl", "qwen3-vl"))
