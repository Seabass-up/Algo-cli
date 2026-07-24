"""Ada's authoritative Echo Veil integration for Algo CLI.

Algo CLI does not implement a second memory store here.  This module is a
bounded compatibility and policy bridge to ``echo_veil.agent_memory``:

* ``AgentMemory`` owns encrypted content, protected retrieval data, lifecycle
  state, crash reconciliation, and key rotation.
* ``AlwaysAvailableMemory`` provides explicitly degraded read-only recall when
  the local embedding service is unavailable.
* ``protection=required`` routes ordinary writes only to Echo Veil and refuses
  plaintext fallback.

The legacy in-memory Oracle wrapper and ``echo_veil_state.json`` plaintext
shadow were removed.  Existing legacy Algo memory files are not silently
migrated or deleted; migration remains an explicit operator action.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

SUPPORTED_ECHO_VEIL_MIN = (0, 5, 0)
SUPPORTED_ECHO_VEIL_MAX_EXCLUSIVE = (0, 6, 0)
DEFAULT_ECHO_PROFILE = "algo-cli-qwen3"
DEFAULT_ECHO_SCOPE = "algo-cli:user"
DEFAULT_ECHO_DIMENSION = 4096
DEFAULT_ECHO_MODEL = "qwen3-embedding:latest"
VALID_PROTECTION_POLICIES = frozenset({"optional", "required"})
_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[.+-].*)?\Z")

AgentMemory: Any = None
AlwaysAvailableMemory: Any = None
EmbeddingUnavailable: Any = None
OllamaTextEmbedder: Any = None


def _distribution_installation_kind(distribution: Any) -> str:
    try:
        raw = distribution.read_text("direct_url.json")
    except (OSError, UnicodeError, ValueError):
        return "direct-url-unpinned"
    if raw is None:
        return "registry-or-wheel"
    if not isinstance(raw, str):
        return "direct-url-unpinned"
    try:
        if not 1 <= len(raw.encode("utf-8")) <= 16_384:
            return "direct-url-unpinned"
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return "direct-url-unpinned"
    if not isinstance(document, dict):
        return "direct-url-unpinned"
    directory = document.get("dir_info")
    if isinstance(directory, dict) and directory.get("editable") is True:
        return "editable"
    vcs = document.get("vcs_info")
    if isinstance(vcs, dict) and re.fullmatch(
        r"[0-9a-fA-F]{40,64}",
        str(vcs.get("commit_id") or ""),
    ):
        return "vcs-pinned"
    archive = document.get("archive_info")
    if isinstance(archive, dict):
        hash_value = str(archive.get("hash") or "")
        hashes = archive.get("hashes")
        sha256_value = str(hashes.get("sha256") or "") if isinstance(hashes, dict) else ""
        if re.fullmatch(r"sha256=[0-9a-fA-F]{64}", hash_value) or re.fullmatch(
            r"[0-9a-fA-F]{64}",
            sha256_value,
        ):
            return "archive-pinned"
    return "direct-url-unpinned"


try:
    _echo_veil_package = importlib.import_module("echo_veil")
    _agent_memory_package = importlib.import_module("echo_veil.agent_memory")
    AgentMemory = getattr(_agent_memory_package, "AgentMemory")
    AlwaysAvailableMemory = getattr(
        _agent_memory_package,
        "AlwaysAvailableMemory",
    )
    EmbeddingUnavailable = getattr(_agent_memory_package, "EmbeddingUnavailable")
    OllamaTextEmbedder = getattr(_agent_memory_package, "OllamaTextEmbedder")
    ECHO_VEIL_AVAILABLE = True
    ECHO_VEIL_IMPORT_ERROR = ""
    ECHO_VEIL_SOURCE_VERSION = str(getattr(_echo_veil_package, "__version__", "") or "")
    try:
        _echo_veil_distribution = importlib.metadata.distribution("echo-veil")
        ECHO_VEIL_DISTRIBUTION_VERSION = _echo_veil_distribution.version
        ECHO_VEIL_INSTALLATION_KIND = _distribution_installation_kind(_echo_veil_distribution)
    except importlib.metadata.PackageNotFoundError:
        ECHO_VEIL_DISTRIBUTION_VERSION = ""
        ECHO_VEIL_INSTALLATION_KIND = "missing"
except (ImportError, AttributeError) as exc:
    ECHO_VEIL_AVAILABLE = False
    ECHO_VEIL_IMPORT_ERROR = type(exc).__name__
    ECHO_VEIL_SOURCE_VERSION = ""
    ECHO_VEIL_DISTRIBUTION_VERSION = ""
    ECHO_VEIL_INSTALLATION_KIND = "missing"


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _version_supported() -> bool:
    source = _version_tuple(ECHO_VEIL_SOURCE_VERSION)
    distribution = _version_tuple(ECHO_VEIL_DISTRIBUTION_VERSION)
    return bool(
        ECHO_VEIL_AVAILABLE
        and source is not None
        and distribution is not None
        and source == distribution
        and ECHO_VEIL_INSTALLATION_KIND in {"registry-or-wheel", "archive-pinned", "vcs-pinned"}
        and SUPPORTED_ECHO_VEIL_MIN <= source < SUPPORTED_ECHO_VEIL_MAX_EXCLUSIVE
    )


def _load_config_mapping() -> dict[str, Any]:
    path = CONFIG_DIR / "config.json"
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def protection_policy(config: object) -> str:
    raw = (
        config.get("echo_veil_protection", "optional")
        if isinstance(config, dict)
        else getattr(config, "echo_veil_protection", "optional")
    )
    value = str(raw or "optional").strip().casefold()
    if value not in VALID_PROTECTION_POLICIES:
        raise ValueError("echo_veil_protection must be optional or required")
    return value


def protection_required(config: object) -> bool:
    return protection_policy(config) == "required"


def _config_value(config: object, name: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def _state_dir(config: object) -> Path:
    configured = str(_config_value(config, "echo_veil_state_dir", "") or "").strip()
    return Path(configured).expanduser() if configured else CONFIG_DIR / "echo-veil"


def _profile(config: object) -> str:
    return str(_config_value(config, "echo_veil_profile", DEFAULT_ECHO_PROFILE) or DEFAULT_ECHO_PROFILE)


def _scope(config: object) -> str:
    return str(_config_value(config, "echo_veil_scope", DEFAULT_ECHO_SCOPE) or DEFAULT_ECHO_SCOPE)


def _enabled(config: object) -> bool:
    return bool(_config_value(config, "echo_veil_enabled", False))


class EchoVeilMemoryLayer:
    """Thin lifecycle wrapper around Echo Veil's concrete host adapter."""

    def __init__(self, config: object) -> None:
        if not ECHO_VEIL_AVAILABLE:
            raise RuntimeError("Echo Veil is not installed in the Algo CLI runtime")
        if not _version_supported():
            raise RuntimeError(
                "Echo Veil must be a matching, supported, non-editable pinned distribution in the >=0.5.0,<0.6.0 range"
            )
        if not _enabled(config):
            raise RuntimeError("Echo Veil is not enabled")
        self.config = config
        self.profile = _profile(config)
        self.scope = _scope(config)
        self.state_dir = _state_dir(config)
        self.degraded = False
        model = str(_config_value(config, "harness_embed_model", DEFAULT_ECHO_MODEL) or DEFAULT_ECHO_MODEL)
        dimension_raw = _config_value(
            config,
            "echo_veil_embedding_dimension",
            _config_value(config, "embed_dimensions", DEFAULT_ECHO_DIMENSION),
        )
        dimension = DEFAULT_ECHO_DIMENSION if dimension_raw is None else int(dimension_raw)
        base_url = str(_config_value(config, "host", "http://127.0.0.1:11434") or "http://127.0.0.1:11434")
        capacity = int(_config_value(config, "echo_veil_capacity", 400))
        try:
            embedder = OllamaTextEmbedder(
                model=model,
                base_url=base_url,
                dimension=dimension,
            )
            self.memory = AgentMemory(
                self.state_dir,
                profile=self.profile,
                scope=self.scope,
                capacity=capacity,
                embed=embedder,
            )
        except EmbeddingUnavailable:
            self.memory = AlwaysAvailableMemory(
                self.state_dir,
                profile=self.profile,
                scope=self.scope,
                reason="algo_cli_embedding_service_unavailable",
            )
            self.degraded = True
        try:
            self._validate_security_boundary()
        except Exception:
            self.memory.close()
            raise

    @property
    def writes_available(self) -> bool:
        return not self.degraded

    def remember(
        self,
        payload: str,
        *,
        topic: str = "durable user memory",
    ) -> dict[str, Any]:
        if self.degraded:
            raise RuntimeError("Echo Veil writes are blocked while semantic embeddings are unavailable")
        return self.memory.remember(topic, payload)

    def recall(self, query: str, *, top_k: int = 8) -> dict[str, Any]:
        return self.memory.recall(query, top_k=max(2, int(top_k)))

    def forget(self, vine_id: str) -> dict[str, Any]:
        if self.degraded:
            raise RuntimeError("Echo Veil deletion is unavailable in read-only mode")
        return self.memory.forget(vine_id)

    def list_memories(self) -> list[dict[str, Any]]:
        listing = getattr(self.memory, "list_memories", None)
        if not callable(listing):
            raise RuntimeError("the installed Echo Veil version cannot list memories")
        return listing()

    def doctor(self) -> dict[str, Any]:
        report = dict(self.memory.doctor())
        report["degraded"] = self.degraded
        return report

    def close(self) -> None:
        self.memory.close()

    def _validate_security_boundary(self) -> None:
        report = self.memory.doctor()
        if report.get("security_schema") != "scoped-v2" or report.get("scope_bound") is not True:
            raise RuntimeError("Echo Veil profile must be explicitly migrated to scoped-v2")
        if self.degraded:
            if report.get("writes_available") is not False:
                raise RuntimeError("Echo Veil degraded mode is not fail-closed")
            return
        facts = report.get("readiness")
        required_facts = (
            "crypto_initialized",
            "write_wired",
            "index_wired",
            "retrieval_wired",
            "persistence_wired",
            "restart_restored",
        )
        if not isinstance(facts, dict) or not all(facts.get(name) is True for name in required_facts):
            raise RuntimeError("Echo Veil protected data path is incomplete")


_LAYER: EchoVeilMemoryLayer | None = None
_LAYER_FINGERPRINT: tuple[Any, ...] | None = None
_LAST_INITIALIZATION_ERROR: str | None = None


def _fingerprint(config: object) -> tuple[Any, ...]:
    return (
        _enabled(config),
        protection_policy(config),
        _profile(config),
        _scope(config),
        str(_state_dir(config)),
        _config_value(config, "harness_embed_model", DEFAULT_ECHO_MODEL),
        _config_value(config, "echo_veil_embedding_dimension", None),
        _config_value(config, "embed_dimensions", None),
        _config_value(config, "host", "http://127.0.0.1:11434"),
        _config_value(config, "echo_veil_capacity", 400),
    )


def reset_echo_veil_layer() -> None:
    global _LAYER, _LAYER_FINGERPRINT, _LAST_INITIALIZATION_ERROR
    if _LAYER is not None:
        _LAYER.close()
    _LAYER = None
    _LAYER_FINGERPRINT = None
    _LAST_INITIALIZATION_ERROR = None


def create_echo_veil_layer(
    config: object | None = None,
) -> EchoVeilMemoryLayer | None:
    """Return the sole Algo CLI Echo adapter, or ``None`` when disabled."""

    global _LAYER, _LAYER_FINGERPRINT, _LAST_INITIALIZATION_ERROR
    resolved = _load_config_mapping() if config is None else config
    if not _enabled(resolved):
        return None
    fingerprint = _fingerprint(resolved)
    if _LAYER is not None and _LAYER_FINGERPRINT == fingerprint:
        return _LAYER
    if _LAYER is not None:
        _LAYER.close()
        _LAYER = None
    try:
        _LAYER = EchoVeilMemoryLayer(resolved)
    except Exception as exc:
        _LAST_INITIALIZATION_ERROR = type(exc).__name__
        if protection_required(resolved):
            raise RuntimeError("required Echo Veil protection is unavailable; memory writes are blocked") from exc
        logger.warning(
            "Echo Veil optional mode is unavailable; legacy plaintext memory remains active (%s)",
            type(exc).__name__,
        )
        return None
    _LAYER_FINGERPRINT = fingerprint
    _LAST_INITIALIZATION_ERROR = None
    return _LAYER


def remember_with_echo_veil(
    config: object,
    fact: str,
    *,
    source: str = "user_explicit",
) -> bool:
    layer = create_echo_veil_layer(config)
    if layer is None:
        if protection_required(config):
            raise RuntimeError("required Echo Veil protection is unavailable; memory write refused")
        return False
    result = layer.remember(
        str(fact),
        topic=f"algo memory · {str(source or 'user_explicit')}",
    )
    memories = getattr(config, "memories", None)
    if isinstance(memories, list) and str(fact) not in memories:
        memories.append(str(fact))
    return bool(result.get("created", False))


def recall_with_echo_veil(
    config: object,
    query: str,
    *,
    top_k: int = 8,
) -> list[str]:
    layer = create_echo_veil_layer(config)
    if layer is None:
        return []
    response = layer.recall(query, top_k=top_k)
    results = response.get("results")
    if not isinstance(results, list):
        return []
    payloads = [
        str(item["payload"])
        for item in results
        if isinstance(item, dict) and item.get("gated") is False and isinstance(item.get("payload"), str)
    ]
    return list(dict.fromkeys(payloads))


def list_echo_veil_memories(config: object) -> list[dict[str, Any]]:
    layer = create_echo_veil_layer(config)
    return [] if layer is None else layer.list_memories()


def get_echo_veil_readiness(
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return independent, non-leaking readiness facts."""

    resolved = _load_config_mapping() if config is None else config
    base: dict[str, Any] = {
        "installed": ECHO_VEIL_AVAILABLE,
        "package_version": ECHO_VEIL_SOURCE_VERSION or None,
        "distribution_version": ECHO_VEIL_DISTRIBUTION_VERSION or None,
        "version_supported": _version_supported(),
        "enabled": _enabled(resolved),
        "protection_policy": protection_policy(resolved),
        "crypto_initialized": False,
        "write_wired": False,
        "index_wired": False,
        "retrieval_wired": False,
        "persistence_wired": False,
        "restart_restored": False,
        "rotation_ready": False,
        "healthy": False,
        "local_protection_ready": False,
        "production_ready": False,
        "degraded": False,
        "readiness_source": "algo_cli.ada_memory_echo_veil.get_echo_veil_readiness",
        "runtime": (f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}"),
        "installation_identity": (
            ECHO_VEIL_INSTALLATION_KIND if _version_supported() else f"{ECHO_VEIL_INSTALLATION_KIND}-unsupported"
        ),
        "import_error": ECHO_VEIL_IMPORT_ERROR or _LAST_INITIALIZATION_ERROR,
    }
    if not base["enabled"] or not base["version_supported"]:
        return base
    try:
        layer = create_echo_veil_layer(resolved)
        if layer is None:
            return base
        doctor = layer.doctor()
        facts = doctor.get("readiness")
        if isinstance(facts, dict):
            for key in (
                "crypto_initialized",
                "write_wired",
                "index_wired",
                "retrieval_wired",
                "persistence_wired",
                "restart_restored",
                "rotation_ready",
                "healthy",
            ):
                base[key] = bool(facts.get(key, False))
        base["degraded"] = bool(doctor.get("degraded", False))
        base["key_id"] = doctor.get("key_id")
        base["security_schema"] = doctor.get("security_schema")
        base["quarantined_records"] = int(doctor.get("quarantined_records", 0) or 0)
        base["rotation_state"] = (
            doctor.get("rotation", {}).get("state") if isinstance(doctor.get("rotation"), dict) else None
        )
        base["healthy"] = bool(base["healthy"] and base["protection_policy"] == "required" and not base["degraded"])
        base["local_protection_ready"] = base["healthy"]
    except Exception as exc:
        base["import_error"] = type(exc).__name__
    return base
