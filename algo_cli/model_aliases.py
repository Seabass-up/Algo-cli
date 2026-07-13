"""Canonical user-facing model aliases shared by config and provider routing."""

from __future__ import annotations


CODEX_MODEL_ALIASES = {
    "gpt-5.6": "gpt-5.6-sol",
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
    "lunna": "gpt-5.6-luna",
}


def normalize_codex_model(model: str) -> str:
    """Expand a Codex short name while leaving every unrelated model intact."""
    text = str(model or "").strip()
    bare = text.split(":", 1)[0].lower()
    return CODEX_MODEL_ALIASES.get(bare, text)
