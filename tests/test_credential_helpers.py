"""Tests for the credential helper interface (algo_cli.credential_helpers)."""

from __future__ import annotations

from pathlib import Path

import pytest

from algo_cli import credential_helpers as ch


@pytest.fixture(autouse=True)
def clean_registry():
    """Reset the helper registry before each test."""
    saved = dict(ch._REGISTRY)
    ch._REGISTRY.clear()
    ch._init_default_helpers()
    yield
    ch._REGISTRY.clear()
    ch._REGISTRY.update(saved)


@pytest.fixture
def cred_dir(tmp_path: Path, monkeypatch):
    cdir = tmp_path / "credentials"
    cdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ch, "CREDENTIALS_DIR", cdir)
    return cdir


class TestEnvCredentialHelper:
    def test_get_from_env(self, monkeypatch):
        helper = ch.EnvCredentialHelper()
        monkeypatch.setenv("TEST_KEY", "test_value")
        assert helper.get("TEST_KEY") == "test_value"

    def test_get_missing_returns_none(self):
        helper = ch.EnvCredentialHelper()
        assert helper.get("NONEXISTENT_KEY_12345") is None

    def test_store_sets_env(self, monkeypatch):
        helper = ch.EnvCredentialHelper()
        helper.store("MY_KEY", "my_value")
        assert helper.get("MY_KEY") == "my_value"

    def test_erase_removes_env(self, monkeypatch):
        helper = ch.EnvCredentialHelper()
        helper.store("TEMP_KEY", "val")
        helper.erase("TEMP_KEY")
        assert helper.get("TEMP_KEY") is None


class TestFileCredentialHelper:
    def test_store_and_get(self, cred_dir):
        helper = ch.FileCredentialHelper("test-helper", cred_dir)
        helper.store("api_key", "secret123")
        assert helper.get("api_key") == "secret123"

    def test_get_missing(self, cred_dir):
        helper = ch.FileCredentialHelper("test-helper", cred_dir)
        assert helper.get("nonexistent") is None

    def test_erase(self, cred_dir):
        helper = ch.FileCredentialHelper("test-helper", cred_dir)
        helper.store("key1", "val1")
        helper.erase("key1")
        assert helper.get("key1") is None

    def test_list_keys(self, cred_dir):
        helper = ch.FileCredentialHelper("test-helper", cred_dir)
        helper.store("key1", "val1")
        helper.store("key2", "val2")
        keys = helper.list_keys()
        assert set(keys) == {"key1", "key2"}

    def test_overwrite_existing(self, cred_dir):
        helper = ch.FileCredentialHelper("test-helper", cred_dir)
        helper.store("key", "old")
        helper.store("key", "new")
        assert helper.get("key") == "new"

    def test_persists_to_file(self, cred_dir):
        helper1 = ch.FileCredentialHelper("persist-test", cred_dir)
        helper1.store("my_key", "my_value")
        # New helper instance reading same file
        helper2 = ch.FileCredentialHelper("persist-test", cred_dir)
        assert helper2.get("my_key") == "my_value"


class TestOllamaCloudCredentialHelper:
    def test_get_from_env(self, monkeypatch):
        helper = ch.OllamaCloudCredentialHelper()
        monkeypatch.setenv("OLLAMA_API_KEY", "sk-test-123")
        assert helper.get("OLLAMA_API_KEY") == "sk-test-123"

    def test_get_missing_returns_none(self, monkeypatch):
        helper = ch.OllamaCloudCredentialHelper()
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        # Also need to ensure load_runtime_env doesn't find it
        monkeypatch.setattr(ch.os.environ, "get", lambda k, d="": d)
        result = helper.get("OLLAMA_API_KEY")
        # Could be None or empty depending on file fallback
        assert result is None or result == ""

    def test_store_and_retrieve(self, cred_dir, monkeypatch):
        file_helper = ch.FileCredentialHelper("ollama-cloud", cred_dir)
        helper = ch.OllamaCloudCredentialHelper(file_helper)
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        helper.store("OLLAMA_API_KEY", "sk-stored")
        assert helper.get("OLLAMA_API_KEY") == "sk-stored"


class TestXAIOAuthCredentialHelper:
    def test_name(self):
        helper = ch.XAIOAuthCredentialHelper()
        assert helper.name == "xai-oauth"

    def test_store_and_get(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ch, "CONFIG_DIR", tmp_path)
        helper = ch.XAIOAuthCredentialHelper()
        helper.store("access_token", "tok_123")
        assert helper.get("access_token") == "tok_123"

    def test_erase(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ch, "CONFIG_DIR", tmp_path)
        helper = ch.XAIOAuthCredentialHelper()
        helper.store("access_token", "tok_123")
        helper.erase("access_token")
        assert helper.get("access_token") is None

    def test_list_keys(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ch, "CONFIG_DIR", tmp_path)
        helper = ch.XAIOAuthCredentialHelper()
        helper.store("k1", "v1")
        helper.store("k2", "v2")
        assert set(helper.list_keys()) == {"k1", "k2"}


class TestXAIAPICredentialHelper:
    def test_name(self):
        helper = ch.XAIAPICredentialHelper()
        assert helper.name == "xai-api"

    def test_store_and_erase_runtime_key(self, tmp_path: Path, monkeypatch):
        from algo_cli import config

        env_path = tmp_path / "env"
        monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
        monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        helper = ch.XAIAPICredentialHelper()

        helper.store("XAI_API_KEY", "xai-secret")
        assert helper.get("XAI_API_KEY") == "xai-secret"
        assert "xai-secret" in env_path.read_text(encoding="utf-8")

        helper.erase("XAI_API_KEY")
        assert helper.get("XAI_API_KEY") is None


class TestGoogleWorkspaceCredentialHelper:
    def test_name(self):
        helper = ch.GoogleWorkspaceCredentialHelper()
        assert helper.name == "google-workspace"

    def test_store_and_get(self, tmp_path: Path, monkeypatch):
        helper = ch.GoogleWorkspaceCredentialHelper(tmp_path / "google_workspace_auth.json")
        helper.store("access_token", "goog_tok")
        assert helper.get("access_token") == "goog_tok"


class TestRegistry:
    def test_list_helpers_includes_defaults(self):
        names = ch.list_helpers()
        assert "env" in names
        assert "ollama-cloud" in names
        assert "xai-api" in names
        assert "xai-oauth" in names
        assert "google-workspace" in names
        assert "github-token" in names

    def test_register_custom_helper(self):
        class CustomHelper(ch.CredentialHelper):
            @property
            def name(self): return "custom"
            def get(self, key): return "custom_value"
            def store(self, key, value): pass
            def erase(self, key): pass
            def list_keys(self): return []

        ch.register_helper(CustomHelper())
        assert "custom" in ch.list_helpers()
        assert ch.get_credential("custom", "any") == "custom_value"

    def test_get_helper_missing(self):
        assert ch.get_helper("nonexistent") is None

    def test_get_credential_missing_helper(self):
        assert ch.get_credential("nonexistent", "key") is None

    def test_store_credential_missing_helper(self):
        assert ch.store_credential("nonexistent", "key", "val") is False

    def test_erase_credential_missing_helper(self):
        assert ch.erase_credential("nonexistent", "key") is False

    def test_convenience_store_and_get(self, cred_dir):
        # Register a fresh file helper
        helper = ch.FileCredentialHelper("convenience-test", cred_dir)
        ch.register_helper(helper)
        assert ch.store_credential("convenience-test", "k", "v") is True
        assert ch.get_credential("convenience-test", "k") == "v"

    def test_convenience_erase(self, cred_dir):
        helper = ch.FileCredentialHelper("erase-test", cred_dir)
        ch.register_helper(helper)
        ch.store_credential("erase-test", "k", "v")
        assert ch.erase_credential("erase-test", "k") is True
        assert ch.get_credential("erase-test", "k") is None
