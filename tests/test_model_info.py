"""Tests for the model metadata cache (model_info.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from algo_cli import model_info


class _FakeDetails:
    def __init__(self, family="qwen3", params="8.2B", quant="Q4_K_M", families=None):
        self.family = family
        self.families = families if families is not None else [family]
        self.parameter_size = params
        self.quantization_level = quant


class _FakeResponse:
    def __init__(self, family="qwen3", ctx=32768, params="8.2B", quant="Q4_K_M", families=None):
        self.details = _FakeDetails(family=family, params=params, quant=quant, families=families)
        self.model_info = {f"{family}.context_length": ctx, "general.architecture": family}


class _FakeClient:
    def __init__(self, family="qwen3", ctx=32768, params="8.2B", quant="Q4_K_M", families=None):
        self._resp = _FakeResponse(family=family, ctx=ctx, params=params, quant=quant, families=families)

    def show(self, model: str) -> _FakeResponse:
        return self._resp


def test_fetch_model_info_basic():
    info = model_info.fetch_model_info(_FakeClient(), "qwen3:latest")
    assert info["family"] == "qwen3"
    assert info["context_length"] == 32768
    assert info["parameter_size"] == "8.2B"
    assert info["quantization"] == "Q4_K_M"
    assert info["supports_thinking"] is True
    assert info["supports_vision"] is False
    assert info["supports_tools"] is True
    assert "error" not in info


def test_fetch_model_info_vision():
    info = model_info.fetch_model_info(_FakeClient(family="llava", ctx=4096, families=["llava", "clip"]), "llava:latest")
    assert info["supports_vision"] is True
    assert info["supports_thinking"] is False


def test_fetch_model_info_error():
    class _ErrorClient:
        def show(self, model: str):
            raise ConnectionError("offline")

    info = model_info.fetch_model_info(_ErrorClient(), "bad-model")
    assert "error" in info


def test_ensure_model_info_writes_cache(config_dir: Path):
    client = _FakeClient()
    info = model_info.ensure_model_info(client, "qwen3:8b")
    assert info["context_length"] == 32768

    cache_file = config_dir / "model_info" / "qwen3_8b.json"
    assert cache_file.exists()
    loaded = json.loads(cache_file.read_text())
    assert loaded["context_length"] == 32768


def test_ensure_model_info_uses_memory_cache():
    client = _FakeClient()
    info1 = model_info.ensure_model_info(client, "qwen3:latest")

    class _BrokenClient:
        def show(self, model: str):
            raise RuntimeError("should not be called")

    info2 = model_info.ensure_model_info(_BrokenClient(), "qwen3:latest")
    assert info1["context_length"] == info2["context_length"]


def test_write_model_record(config_dir: Path):
    info = {
        "name": "qwen3:latest",
        "family": "qwen3",
        "parameter_size": "8.2B",
        "quantization": "Q4_K_M",
        "context_length": 32768,
        "supports_thinking": True,
        "supports_vision": False,
    }
    model_info.write_model_record("qwen3:latest", info)
    record_path = config_dir / "models" / "qwen3_latest.md"
    assert record_path.exists()
    content = record_path.read_text()
    assert "qwen3" in content
    assert "32,768" in content
    assert "yes" in content  # supports thinking


def test_get_context_length():
    assert model_info.get_context_length({"context_length": 8192}) == 8192
    assert model_info.get_context_length({}) is None
    assert model_info.get_context_length({"context_length": None}) is None


def test_supports_thinking():
    assert model_info.supports_thinking({"supports_thinking": True}) is True
    assert model_info.supports_thinking({"supports_thinking": False}) is False
    # Unknown model (empty info, cloud mode, show() failed): don't suppress thinking.
    assert model_info.supports_thinking({}) is True


def test_safe_name_sanitizes():
    assert model_info._safe_name("qwen3:latest") == "qwen3_latest"
    assert model_info._safe_name("my/model:v2") == "my_model_v2"


def test_is_gemini_model_positive():
    assert model_info.is_gemini_model("gemini-3-flash-preview:cloud") is True
    assert model_info.is_gemini_model("gemini-pro") is True
    assert model_info.is_gemini_model("Gemini-1.5") is True
    assert model_info.is_gemini_model("gemini_2_flash") is True


def test_is_gemini_model_negative():
    assert model_info.is_gemini_model("qwen3:235b-cloud") is False
    assert model_info.is_gemini_model("gpt-oss:120b-cloud") is False
    assert model_info.is_gemini_model("llama3.2") is False
    assert model_info.is_gemini_model("gemma3:27b") is False  # Gemma is open-weight, not Gemini


def test_is_gemini_model_edge_cases():
    assert model_info.is_gemini_model("") is False
    assert model_info.is_gemini_model(None) is False
    assert model_info.is_gemini_model(42) is False
    # "gemini" without a separator is ambiguous — require the dash/underscore
    assert model_info.is_gemini_model("geminix") is False


def test_is_xai_model_positive():
    assert model_info.is_xai_model("grok-4-latest") is True
    assert model_info.is_xai_model("grok-3") is True
    assert model_info.is_xai_model("Grok-4-Heavy") is True
    assert model_info.is_xai_model("grok_2") is True


def test_is_xai_model_negative():
    assert model_info.is_xai_model("qwen3:235b-cloud") is False
    assert model_info.is_xai_model("gpt-oss:120b-cloud") is False
    assert model_info.is_xai_model("gemini-3-flash-preview:cloud") is False
    assert model_info.is_xai_model("llama3.2") is False
    # "grok" without a separator is ambiguous
    assert model_info.is_xai_model("grokai") is False


def test_is_xai_model_edge_cases():
    assert model_info.is_xai_model("") is False
    assert model_info.is_xai_model(None) is False
    assert model_info.is_xai_model(42) is False


def test_synthesize_xai_info_shape():
    info = model_info.synthesize_xai_info("grok-4-latest")
    assert info["name"] == "grok-4-latest"
    assert info["family"] == "grok"
    assert info["provider"] == "xai"
    assert info["supports_tools"] is True
    assert info["context_length"] > 0
    # Must not include an "error" key (would trip caller error paths).
    assert "error" not in info


def test_synthesize_chatgpt_info_uses_model_context_window():
    assert model_info.synthesize_chatgpt_info("gpt-5.6-sol")["context_length"] == 272_000
    assert model_info.synthesize_chatgpt_info("gpt-5.6-terra")["supports_vision"] is True
    assert model_info.synthesize_chatgpt_info("gpt-5.6-luna")["supports_thinking"] is True
    assert model_info.synthesize_chatgpt_info("gpt-5.5")["context_length"] == 1_000_000
    assert model_info.synthesize_chatgpt_info("gpt-5.4")["context_length"] == 1_000_000
    assert model_info.synthesize_chatgpt_info("gpt-5.4-mini")["context_length"] == 400_000


@pytest.mark.parametrize("alias", ["sol", "terra", "luna", "lunna"])
def test_codex_short_aliases_are_chatgpt_models(alias):
    assert model_info.is_chatgpt_model(alias) is True
    info = model_info.synthesize_chatgpt_info(alias)
    assert info["context_length"] == 272_000
    assert info["provider"] == "chatgpt"


def test_cloud_model_hints_minimax():
    hints = model_info.cloud_model_hints("minimax-m3:cloud")
    assert hints["context_length"] == 524_288
    assert hints["supports_thinking"] is True


def test_merge_model_hints_fills_missing_context():
    merged = model_info.merge_model_hints({"name": "minimax-m3:cloud"}, "minimax-m3:cloud")
    assert merged["context_length"] == 524_288


def test_effective_context_limits_caps_user_num_ctx():
    class _Cfg:
        num_ctx = 8192

    runtime, native = model_info.effective_context_limits(
        _Cfg(),
        {"context_length": 524_288},
    )
    assert runtime == 8192
    assert native == 524_288


def test_resolve_model_info_cloud_without_client():
    class _Cfg:
        model = "minimax-m3:cloud"
        cloud = True

    info = model_info.resolve_model_info(_Cfg(), None)
    assert info["context_length"] == 524_288


def test_parse_ollama_show_output_minimax():
    text = """
  Model
    architecture        minimax-m3
    parameters          0
    context length      524288
    embedding length    0

  Capabilities
    completion
    tools
    thinking
    vision
"""
    parsed = model_info.parse_ollama_show_output(text, model="minimax-m3:cloud")
    assert parsed["context_length"] == 524_288
    assert parsed["family"] == "minimax-m3"
    assert parsed["supports_thinking"] is True
    assert parsed["supports_vision"] is True
    assert parsed["supports_tools"] is True


def test_parse_ollama_show_output_ignores_capabilities_as_quantization():
    text = """
  Model
    architecture        glm5.2
    parameters          756162687872
    context length      1000000
    embedding length    0
    quantization        Capabilities

  Capabilities
    thinking
    completion
    tools
"""
    parsed = model_info.parse_ollama_show_output(text, model="glm-5.2:cloud")

    assert parsed["quantization"] == ""
    assert parsed["capabilities"] == ["completion", "thinking", "tools"]
    assert parsed["supports_thinking"] is True
    assert parsed["supports_tools"] is True


def test_parse_ollama_show_output_accepts_valid_quantization_and_inline_capabilities():
    text = """
  Model
    architecture        qwen3
    parameters          8.2B
    context length      32768
    quantization        Q4_K_M
  Capabilities          completion tools thinking
"""
    parsed = model_info.parse_ollama_show_output(text, model="qwen3:8b")

    assert parsed["quantization"] == "Q4_K_M"
    assert parsed["capabilities"] == ["completion", "thinking", "tools"]
    assert parsed["supports_thinking"] is True
    assert parsed["supports_tools"] is True


def test_show_name_candidates_adds_cloud_tag():
    names = model_info._show_name_candidates("minimax-m3", cloud=True)
    assert names == ["minimax-m3", "minimax-m3:cloud"]


def test_fetch_model_info_from_cli(monkeypatch):
    sample = "  Model\n    architecture glm5.1\n    context length 202752\n  Capabilities\n    thinking\n    tools\n"

    class _Proc:
        returncode = 0
        stdout = sample
        stderr = ""

    monkeypatch.setattr(model_info.shutil, "which", lambda _name: "ollama")
    monkeypatch.setattr(model_info.subprocess, "run", lambda *a, **k: _Proc())
    info = model_info.fetch_model_info_from_cli("glm-5.1", cloud=True)
    assert info is not None
    assert info["context_length"] == 202_752
    assert info["source"] == "ollama-show"
