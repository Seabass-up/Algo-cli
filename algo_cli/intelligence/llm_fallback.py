"""H15 — LLM Fallback Chain.

Multi-tier fallback for LLM calls: primary model → fallback model →
3-tier JSON parsing. Prevents total failure when primary model is unavailable.
Mined from T3MP3ST WHITEPAPER §5.2 safeLLMCall().
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class FallbackResult:
    """Result of a fallback chain call."""

    success: bool
    response: str = ""
    parsed_json: dict | None = None
    model_used: str = ""
    parse_tier: int = 0  # 0=direct, 1=regex, 2=repair, 3=failed
    error: str = ""


def _parse_json_tier_1(text: str) -> dict | None:
    """Tier 1: Direct json.loads."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_json_tier_2(text: str) -> dict | None:
    """Tier 2: Extract JSON from markdown code blocks or surrounding text."""
    # Try to find ```json ... ``` blocks
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        result = _parse_json_tier_1(match.group(1).strip())
        if result is not None:
            return result

    # Try to find the first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        result = _parse_json_tier_1(match.group(0))
        if result is not None:
            return result

    return None


def _parse_json_tier_3(text: str) -> dict | None:
    """Tier 3: Repair common JSON errors (trailing commas, unquoted keys)."""
    repaired = text

    # Remove trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # Quote unquoted keys (key:)
    repaired = re.sub(r"(\w+)\s*:", r'"\1":', repaired)
    # But don't double-quote already-quoted keys
    repaired = re.sub(r'""(\w+)""\s*:', r'"\1":', repaired)

    # Remove control characters
    repaired = re.sub(r"[\x00-\x1f]", "", repaired)

    return _parse_json_tier_1(repaired)


def parse_json_with_fallback(text: str) -> tuple[dict | None, int]:
    """Parse JSON with 3-tier fallback.

    Returns (parsed_dict_or_None, tier_used).
    Tier 0 = direct parse, 1 = regex extraction, 2 = repair, 3 = failed.
    """
    result = _parse_json_tier_1(text)
    if result is not None:
        return result, 0

    result = _parse_json_tier_2(text)
    if result is not None:
        return result, 1

    result = _parse_json_tier_3(text)
    if result is not None:
        return result, 2

    return None, 3


def llm_call_with_fallback(
    prompt: str,
    primary_client: Any | None = None,
    fallback_client: Any | None = None,
    expect_json: bool = False,
) -> FallbackResult:
    """Call LLM with fallback chain.

    Args:
        prompt: The prompt to send.
        primary_client: Primary model client (must have .generate(prompt) -> str).
        fallback_client: Fallback model client.
        expect_json: If True, attempt JSON parsing with 3-tier fallback.

    Returns:
        FallbackResult with success status and parsed data.
    """
    # Try primary model
    if primary_client is not None:
        try:
            response = primary_client.generate(prompt)
            if response:
                if expect_json:
                    parsed, tier = parse_json_with_fallback(response)
                    if parsed is not None:
                        return FallbackResult(
                            success=True,
                            response=response,
                            parsed_json=parsed,
                            model_used=getattr(primary_client, "model_name", "primary"),
                            parse_tier=tier,
                        )
                else:
                    return FallbackResult(
                        success=True,
                        response=response,
                        model_used=getattr(primary_client, "model_name", "primary"),
                    )
        except Exception as e:
            primary_error = str(e)
        else:
            primary_error = "empty response"
    else:
        primary_error = "no primary client"

    # Try fallback model
    if fallback_client is not None:
        try:
            response = fallback_client.generate(prompt)
            if response:
                if expect_json:
                    parsed, tier = parse_json_with_fallback(response)
                    if parsed is not None:
                        return FallbackResult(
                            success=True,
                            response=response,
                            parsed_json=parsed,
                            model_used=getattr(fallback_client, "model_name", "fallback"),
                            parse_tier=tier,
                        )
                else:
                    return FallbackResult(
                        success=True,
                        response=response,
                        model_used=getattr(fallback_client, "model_name", "fallback"),
                    )
        except Exception as e:
            fallback_error = str(e)
        else:
            fallback_error = "empty response"
    else:
        fallback_error = "no fallback client"

    return FallbackResult(
        success=False,
        error=f"Primary: {primary_error}; Fallback: {fallback_error}",
    )
