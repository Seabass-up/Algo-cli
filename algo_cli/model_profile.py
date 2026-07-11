"""Model-aware runtime profiles.

The harness knows each model's parameter size and provider but historically
applied one static set of knobs (num_ctx, temperature, reflection cadence) to
every model. A 4B local model and a 671B cloud model have very different needs:
small models want a tighter window, cooler sampling, and more frequent
reflection; large/cloud models can take a wider window and lighter supervision.

This module derives a :class:`ModelProfile` from model metadata. It only fills
in values the user has NOT explicitly changed from the Config defaults, so an
explicit ``/ctx``, ``/temp``, or ``/thinkevery`` always wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import model_info as _model_info_module

# Config-default sentinels. A field still equal to its default is treated as
# "untouched" and therefore eligible for adaptation. Kept in sync with
# config.Config; a mismatch only means adaptation is slightly more conservative.
DEFAULT_NUM_CTX = 8192
DEFAULT_TEMPERATURE = 0.4
DEFAULT_TOOL_THINK_EVERY = 10

# Size bands in billions of parameters.
SMALL_MAX_B = 9.0      # <=9B  -> small
MEDIUM_MAX_B = 32.0    # <=32B -> medium; above -> large


@dataclass(frozen=True)
class ModelProfile:
    size_class: str            # "small" | "medium" | "large" | "unknown"
    provider: str              # "local" | "cloud" | "xai" | "chatgpt"
    num_ctx: int
    temperature: float
    tool_think_every: int
    note: str                  # short human-readable rationale


def _size_class(size_b: float | None) -> str:
    if size_b is None:
        return "unknown"
    if size_b <= SMALL_MAX_B:
        return "small"
    if size_b <= MEDIUM_MAX_B:
        return "medium"
    return "large"


def _provider(cfg: Any) -> str:
    model = getattr(cfg, "model", "")
    if _model_info_module.is_xai_model(model):
        return "xai"
    if _model_info_module.is_chatgpt_model(model):
        return "chatgpt"
    return "cloud" if getattr(cfg, "cloud", False) else "local"


def recommend_profile(cfg: Any, model_info: dict[str, Any] | None) -> ModelProfile:
    """Compute a recommended profile for the active model.

    The recommendation is bounded by the model's real native context window
    when known, so we never recommend a window the model cannot serve.
    """
    info = model_info or {}
    size_b = _model_info_module.parameter_size_billions(info)
    size_class = _size_class(size_b)
    provider = _provider(cfg)
    native_ctx = _model_info_module.get_context_length(info)

    # Baseline recommendations by size class. Native context is a ceiling, not
    # a default allocation: requesting a 128K-1M window for a short task wastes
    # KV-cache memory and can sharply increase local prefill latency. Explicit
    # /ctx user overrides still opt into wider windows in effective_params().
    if size_class == "small":
        num_ctx, temperature, think_every = 8192, 0.3, 6
        note = "small model: tight fallback window, cooler sampling, frequent reflection"
    elif size_class == "medium":
        num_ctx, temperature, think_every = 16384, 0.4, 10
        note = "medium model: standard fallback window and supervision"
    elif size_class == "large":
        num_ctx, temperature, think_every = 32768, 0.5, 14
        note = "large model: wider fallback window, lighter supervision"
    else:  # unknown
        # Remote models often report no size; assume capable but be moderate.
        if provider in {"cloud", "xai", "chatgpt"}:
            num_ctx, temperature, think_every = 32768, 0.4, 12
            note = "unknown-size remote model: moderate-wide fallback window"
        else:
            num_ctx, temperature, think_every = DEFAULT_NUM_CTX, DEFAULT_TEMPERATURE, DEFAULT_TOOL_THINK_EVERY
            note = "unknown model: conservative fallback defaults"

    if isinstance(native_ctx, int) and native_ctx > 0:
        if provider == "local":
            num_ctx = min(num_ctx, native_ctx)
            note = f"{note}; local allocation capped by native context"
        else:
            num_ctx = native_ctx
            note = f"{note}; remote native context"

    return ModelProfile(
        size_class=size_class,
        provider=provider,
        num_ctx=num_ctx,
        temperature=temperature,
        tool_think_every=think_every,
        note=note,
    )


@dataclass(frozen=True)
class EffectiveParams:
    """The values to actually use this turn, after honoring user overrides."""

    num_ctx: int
    temperature: float
    tool_think_every: int
    adapted_fields: tuple[str, ...]


def effective_params(cfg: Any, model_info: dict[str, Any] | None) -> EffectiveParams:
    """Resolve per-turn params: user overrides win, else the model profile.

    A Config field still equal to its default is considered untouched and is
    replaced by the profile recommendation. Any field the user changed via
    ``/ctx``, ``/temp``, or ``/thinkevery`` is preserved exactly.
    """
    profile = recommend_profile(cfg, model_info)
    adapted: list[str] = []

    if int(getattr(cfg, "num_ctx", DEFAULT_NUM_CTX)) == DEFAULT_NUM_CTX:
        num_ctx = profile.num_ctx
        if num_ctx != DEFAULT_NUM_CTX:
            adapted.append("num_ctx")
    else:
        num_ctx = int(cfg.num_ctx)

    if float(getattr(cfg, "temperature", DEFAULT_TEMPERATURE)) == DEFAULT_TEMPERATURE:
        temperature = profile.temperature
        if temperature != DEFAULT_TEMPERATURE:
            adapted.append("temperature")
    else:
        temperature = float(cfg.temperature)

    if int(getattr(cfg, "tool_think_every", DEFAULT_TOOL_THINK_EVERY)) == DEFAULT_TOOL_THINK_EVERY:
        think_every = profile.tool_think_every
        if think_every != DEFAULT_TOOL_THINK_EVERY:
            adapted.append("tool_think_every")
    else:
        think_every = int(cfg.tool_think_every)

    return EffectiveParams(
        num_ctx=max(1, num_ctx),
        temperature=temperature,
        tool_think_every=max(1, think_every),
        adapted_fields=tuple(adapted),
    )
