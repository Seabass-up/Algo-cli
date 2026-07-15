"""Credential helper interface for Algo CLI.

Provides a standard get/store/erase interface for credential backends,
inspired by Docker's docker-credential-<name> pattern.

Each credential helper is a Python class that implements:
  - get(key: str) -> str | None
  - store(key: str, value: str) -> None
  - erase(key: str) -> None
  - list_keys() -> list[str]

Built-in helpers:
  - ollama-cloud    (OLLAMA_API_KEY)
  - xai-api         (XAI_API_KEY)
  - xai-oauth       (legacy xAI OAuth tokens; not used by the runtime)
  - google-workspace (Google Workspace OAuth tokens)
  - github-token    (GitHub API token)
  - env             (fallback: read from environment variables)

Custom helpers can be registered at runtime or discovered from
~/.algo_cli/credentials/ as Python modules with a create_helper() function.
"""
from __future__ import annotations

import abc
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text

logger = logging.getLogger(__name__)

CREDENTIALS_DIR = CONFIG_DIR / "credentials"


class CredentialHelper(abc.ABC):
    """Abstract base class for credential helpers."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Helper identifier (e.g. 'ollama-cloud')."""
        ...

    @property
    def description(self) -> str:
        """Short safe description for list/status UIs."""
        doc = type(self).__doc__ or "Credential helper"
        return " ".join(doc.strip().splitlines()[:1])

    @abc.abstractmethod
    def get(self, key: str) -> str | None:
        """Retrieve a credential value by key. Returns None if not found."""
        ...

    @abc.abstractmethod
    def store(self, key: str, value: str) -> None:
        """Store a credential value."""
        ...

    @abc.abstractmethod
    def erase(self, key: str) -> None:
        """Delete a credential by key."""
        ...

    @abc.abstractmethod
    def list_keys(self) -> list[str]:
        """List all stored credential keys."""
        ...


class EnvCredentialHelper(CredentialHelper):
    """Fallback helper that reads from environment variables."""

    @property
    def name(self) -> str:
        return "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key)

    def store(self, key: str, value: str) -> None:
        os.environ[key] = value

    def erase(self, key: str) -> None:
        os.environ.pop(key, None)

    def list_keys(self) -> list[str]:
        # Can't meaningfully list env vars as credential keys
        return []


class FileCredentialHelper(CredentialHelper):
    """File-based credential helper storing JSON in ~/.algo_cli/credentials/.

    Each helper gets its own JSON file: <name>.json
    Credentials are stored as {key: value} pairs.
    """

    def __init__(self, helper_name: str, base_dir: Path | None = None):
        self._name = helper_name
        self._base_dir = base_dir or CREDENTIALS_DIR
        self._file = self._base_dir / f"{helper_name}.json"

    @property
    def name(self) -> str:
        return self._name

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._file, json.dumps(data, indent=2))
        try:
            os.chmod(self._file, 0o600)
        except (OSError, NotImplementedError):
            pass

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def store(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self._save(data)

    def erase(self, key: str) -> None:
        data = self._load()
        data.pop(key, None)
        self._save(data)

    def list_keys(self) -> list[str]:
        return list(self._load().keys())


class OllamaCloudCredentialHelper(CredentialHelper):
    """Credential helper for OLLAMA_API_KEY.

    Reads from environment first, falls back to the runtime env file,
    then to the file-based credential store.
    """

    def __init__(self, file_helper: FileCredentialHelper | None = None):
        self._file_helper = file_helper or FileCredentialHelper("ollama-cloud")

    @property
    def name(self) -> str:
        return "ollama-cloud"

    def get(self, key: str) -> str | None:
        if key == "OLLAMA_API_KEY":
            # Check env first
            val = os.environ.get("OLLAMA_API_KEY", "").strip()
            if val:
                return val
            # Check runtime env file
            try:
                from .config import load_runtime_env
                load_runtime_env(override=True)
                val = os.environ.get("OLLAMA_API_KEY", "").strip()
                if val:
                    return val
            except Exception:
                pass
            # Fall back to file store
            return self._file_helper.get(key)
        return self._file_helper.get(key)

    def store(self, key: str, value: str) -> None:
        self._file_helper.store(key, value)

    def erase(self, key: str) -> None:
        self._file_helper.erase(key)

    def list_keys(self) -> list[str]:
        return self._file_helper.list_keys()


class XAIAPICredentialHelper(CredentialHelper):
    """Credential helper for the documented XAI_API_KEY runtime setting."""

    @property
    def name(self) -> str:
        return "xai-api"

    def get(self, key: str) -> str | None:
        if key != "XAI_API_KEY":
            return None
        try:
            from .config import load_runtime_env

            load_runtime_env(override=True)
        except Exception:
            pass
        return os.environ.get(key, "").strip() or None

    def store(self, key: str, value: str) -> None:
        if key != "XAI_API_KEY":
            raise ValueError("xai-api only manages XAI_API_KEY")
        from .config import update_runtime_env

        update_runtime_env({key: value})

    def erase(self, key: str) -> None:
        if key != "XAI_API_KEY":
            return
        from .config import update_runtime_env

        update_runtime_env({key: None})

    def list_keys(self) -> list[str]:
        return ["XAI_API_KEY"] if self.get("XAI_API_KEY") else []


class XAIOAuthCredentialHelper(CredentialHelper):
    """Legacy helper for xAI OAuth tokens retained only for migration.

    The xAI API runtime no longer reads this file.  Keeping it inspectable lets
    users remove old state without silently treating a browser token as an API
    credential.
    """

    def __init__(self):
        self._auth_file = CONFIG_DIR / "xai_auth.json"

    @property
    def name(self) -> str:
        return "xai-oauth"

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._auth_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str) -> str | None:
        data = self._load()
        return data.get(key)

    def store(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._auth_file, json.dumps(data, indent=2))

    def erase(self, key: str) -> None:
        data = self._load()
        data.pop(key, None)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._auth_file, json.dumps(data, indent=2))

    def list_keys(self) -> list[str]:
        return list(self._load().keys())


class GoogleWorkspaceCredentialHelper(CredentialHelper):
    """Credential helper for Google Workspace OAuth tokens.

    Delegates to the existing google_workspace token file.
    """

    def __init__(self, auth_file: Path | None = None):
        self._auth_file = auth_file

    @property
    def name(self) -> str:
        return "google-workspace"

    @property
    def _path(self) -> Path:
        if self._auth_file is not None:
            return self._auth_file
        # Keep the helper and the active OAuth client on one file.  The old
        # google_workspace_tokens.json path was disconnected from the runtime,
        # so helper writes never affected Google authentication.
        from . import google_workspace_auth

        return google_workspace_auth.AUTH_FILE

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def store(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass

    def erase(self, key: str) -> None:
        data = self._load()
        data.pop(key, None)
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass

    def list_keys(self) -> list[str]:
        return list(self._load().keys())


# --- Registry ---

_REGISTRY: dict[str, CredentialHelper] = {}


def register_helper(helper: CredentialHelper) -> None:
    """Register a credential helper."""
    _REGISTRY[helper.name] = helper


def get_helper(name: str) -> CredentialHelper | None:
    """Get a registered credential helper by name."""
    return _REGISTRY.get(name)


def list_helpers() -> list[str]:
    """List all registered credential helper names."""
    return sorted(_REGISTRY.keys())


def get_credential(helper_name: str, key: str) -> str | None:
    """Convenience: get a credential via a named helper."""
    helper = get_helper(helper_name)
    if helper is None:
        return None
    return helper.get(key)


def store_credential(helper_name: str, key: str, value: str) -> bool:
    """Convenience: store a credential via a named helper. Returns False if helper not found."""
    helper = get_helper(helper_name)
    if helper is None:
        return False
    helper.store(key, value)
    return True


def erase_credential(helper_name: str, key: str) -> bool:
    """Convenience: erase a credential via a named helper. Returns False if helper not found."""
    helper = get_helper(helper_name)
    if helper is None:
        return False
    helper.erase(key)
    return True


def _init_default_helpers() -> None:
    """Register built-in credential helpers."""
    register_helper(EnvCredentialHelper())
    register_helper(FileCredentialHelper("github-token"))
    register_helper(OllamaCloudCredentialHelper())
    register_helper(XAIAPICredentialHelper())
    register_helper(XAIOAuthCredentialHelper())
    register_helper(GoogleWorkspaceCredentialHelper())


# Initialize on import
_init_default_helpers()
