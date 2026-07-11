"""Tests for H15 — LLM Fallback Chain."""
from __future__ import annotations

from algo_cli.intelligence.llm_fallback import (
    parse_json_with_fallback,
    llm_call_with_fallback,
)


def test_parse_json_tier_0_direct() -> None:
    text = '{"key": "value"}'
    result, tier = parse_json_with_fallback(text)

    assert result == {"key": "value"}
    assert tier == 0


def test_parse_json_tier_1_markdown_block() -> None:
    text = 'Here is the JSON:\n```json\n{"key": "value"}\n```\nDone.'
    result, tier = parse_json_with_fallback(text)

    assert result == {"key": "value"}
    assert tier == 1


def test_parse_json_tier_1_embedded() -> None:
    text = 'Some text {"key": "value"} more text'
    result, tier = parse_json_with_fallback(text)

    assert result == {"key": "value"}
    assert tier == 1


def test_parse_json_tier_2_repair_trailing_comma() -> None:
    text = '{"key": "value",}'
    result, tier = parse_json_with_fallback(text)

    assert result == {"key": "value"}
    assert tier == 2


def test_parse_json_tier_3_failed() -> None:
    text = "not json at all"
    result, tier = parse_json_with_fallback(text)

    assert result is None
    assert tier == 3


class _MockClient:
    def __init__(self, response: str, model_name: str = "test", raise_error: bool = False):
        self._response = response
        self.model_name = model_name
        self._raise = raise_error

    def generate(self, prompt: str) -> str:
        if self._raise:
            raise RuntimeError("mock error")
        return self._response


def test_llm_call_primary_success() -> None:
    primary = _MockClient("Hello world")
    result = llm_call_with_fallback("test prompt", primary_client=primary)

    assert result.success is True
    assert result.response == "Hello world"
    assert result.model_used == "test"


def test_llm_call_fallback_success() -> None:
    primary = _MockClient("", raise_error=True)
    fallback = _MockClient("Fallback works", model_name="fallback")
    result = llm_call_with_fallback("test", primary_client=primary, fallback_client=fallback)

    assert result.success is True
    assert result.response == "Fallback works"
    assert result.model_used == "fallback"


def test_llm_call_both_fail() -> None:
    primary = _MockClient("", raise_error=True)
    fallback = _MockClient("", raise_error=True)
    result = llm_call_with_fallback("test", primary_client=primary, fallback_client=fallback)

    assert result.success is False
    assert "Primary" in result.error
    assert "Fallback" in result.error


def test_llm_call_json_parsing() -> None:
    primary = _MockClient('```json\n{"answer": 42}\n```')
    result = llm_call_with_fallback("test", primary_client=primary, expect_json=True)

    assert result.success is True
    assert result.parsed_json == {"answer": 42}
    assert result.parse_tier == 1


def test_llm_call_no_clients() -> None:
    result = llm_call_with_fallback("test")

    assert result.success is False
