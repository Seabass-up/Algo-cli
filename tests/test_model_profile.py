"""Tests for model-aware runtime profiles."""

from algo_cli import model_profile as mp
from algo_cli.config import Config


def test_small_model_tightens():
    cfg = Config()
    params = mp.effective_params(cfg, {"parameter_size": "4B"})
    assert params.temperature == 0.3
    assert params.tool_think_every == 6
    assert "temperature" in params.adapted_fields


def test_large_model_widens_when_native_context_unknown():
    cfg = Config()
    params = mp.effective_params(cfg, {"parameter_size": "671B"})
    assert params.num_ctx == 32768
    assert params.temperature == 0.5
    assert "num_ctx" in params.adapted_fields


def test_known_native_context_is_a_ceiling_not_default_allocation():
    cfg = Config()
    params = mp.effective_params(cfg, {"parameter_size": "671B", "context_length": 131072})
    assert params.num_ctx == 32768
    assert "num_ctx" in params.adapted_fields


def test_gemma4_12b_uses_medium_default_below_native_context():
    cfg = Config(model="gemma4:12b-mlx-bf16")
    params = mp.effective_params(
        cfg,
        {"family": "gemma4_unified", "parameter_size": "12.4B", "context_length": 262144},
    )
    assert params.num_ctx == 16384
    assert params.temperature == 0.4


def test_gemma4_edge_variant_uses_small_default_below_native_context():
    cfg = Config(model="gemma4:e4b-mlx")
    params = mp.effective_params(
        cfg,
        {"family": "gemma4_unified", "parameter_size": "4.5B", "context_length": 131072},
    )
    assert params.num_ctx == 8192
    assert params.temperature == 0.3


def test_user_override_is_honored():
    cfg = Config()
    cfg.num_ctx = 4096
    cfg.temperature = 0.9
    params = mp.effective_params(cfg, {"parameter_size": "4B"})
    assert params.num_ctx == 4096
    assert params.temperature == 0.9
    assert "num_ctx" not in params.adapted_fields
    assert "temperature" not in params.adapted_fields


def test_unknown_local_model_is_conservative():
    cfg = Config()
    profile = mp.recommend_profile(cfg, {})
    assert profile.size_class == "unknown"
    assert profile.provider == "local"


def test_unknown_cloud_model_moderate():
    cfg = Config()
    cfg.cloud = True
    profile = mp.recommend_profile(cfg, {})
    assert profile.provider == "cloud"
    assert profile.num_ctx >= 16384


def test_unknown_chatgpt_model_uses_cloud_sized_window():
    cfg = Config(model="gpt-5.5")
    profile = mp.recommend_profile(cfg, {})
    assert profile.provider == "chatgpt"
    assert profile.num_ctx >= 16384


def test_known_remote_model_uses_native_context_window():
    cfg = Config(model="glm-5.2:cloud", cloud=True)
    profile = mp.recommend_profile(
        cfg,
        {"parameter_size": "756B", "context_length": 1_000_000},
    )
    assert profile.provider == "cloud"
    assert profile.num_ctx == 1_000_000
