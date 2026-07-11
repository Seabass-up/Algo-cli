"""Echo Veil integration for algo-cli tiered memory.

Wraps Echo Veil's Oracle as a decay-driven memory layer that sits between the
harness index and retrieval. Memories are managed through active/compressed/
archived tiers with proximity-based decay, preserving recent context while
compressing older data.

Usage:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer
    
    # Initialize with embedding function and capacity
    layer = EchoVeilMemoryLayer(
        embed_fn=embed_fn,
        capacity=400,
        crypto_key=None  # or AesGcmCryptoShield key for production
    )
    
    # Before retrieval: enrich context with active memories
    enriched_context = layer.observe(query_embedding)
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import echo_veil as _echo_veil_package
    from echo_veil import Oracle, WorkspaceConfig, AesGcmCryptoShield
    ECHO_VEIL_AVAILABLE = True
    ECHO_VEIL_IMPORT_ERROR = ""
    ECHO_VEIL_MODULE_ORIGIN = str(getattr(_echo_veil_package, "__file__", "") or "")
except ImportError as exc:
    ECHO_VEIL_AVAILABLE = False
    ECHO_VEIL_IMPORT_ERROR = type(exc).__name__
    ECHO_VEIL_MODULE_ORIGIN = ""
    Oracle = None  # type: ignore
    WorkspaceConfig = None  # type: ignore
    AesGcmCryptoShield = None  # type: ignore

from .config import CONFIG_DIR, _atomic_write_text

logger = logging.getLogger(__name__)


def get_echo_veil_readiness(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report the optional integration's current, deliberately narrow wiring.

    Persisted state currently restores text metadata for diagnostics, not the
    Oracle's memory tiers. Likewise, the runtime does not yet feed memories to
    ``sprout`` or inject ``observe`` results into retrieval. Keep those stages
    false until the corresponding end-to-end paths exist.
    """
    if config is None:
        config_path = CONFIG_DIR / "config.json"
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        config = loaded if isinstance(loaded, dict) else {}

    return {
        "installed": ECHO_VEIL_AVAILABLE,
        "enabled": bool(config.get("echo_veil_enabled", False)),
        "write_wired": False,
        "retrieval_wired": False,
        "persistence_wired": False,
        "readiness_source": "algo_cli.memory_echo_veil.get_echo_veil_readiness",
        "runtime": f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}",
        "module_origin": ECHO_VEIL_MODULE_ORIGIN or None,
        "import_error": ECHO_VEIL_IMPORT_ERROR or None,
    }


def _memory_records(raw: Any) -> list[dict[str, str]]:
    if raw is None or isinstance(raw, (str, bytes, int, float, bool)):
        return []
    try:
        iterator = iter(raw)
    except TypeError:
        return []

    records: list[dict[str, str]] = []
    for item in iterator:
        topic = item.get("topic") if isinstance(item, dict) else getattr(item, "topic", None)
        if topic is None:
            topic = item.get("title") if isinstance(item, dict) else getattr(item, "title", None)
        if topic is not None:
            records.append({"topic": str(topic)})
    return records


def _tier_count(raw: Any, records: list[dict[str, str]]) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if raw is None or isinstance(raw, (str, bytes, float, bool)):
        return len(records)
    try:
        return len(raw)
    except TypeError:
        return len(records)


class EchoVeilMemoryLayer:
    """Tiered memory layer using Echo Veil's decay-driven architecture.
    
    Manages memories through active/compressed/archived tiers with proximity-
    based decay. Recent context stays active; older data is compressed and 
    eventually archived while preserving structured tension for contradictions.
    """
    
    def __init__(
        self,
        embed_fn: Callable[[list[str]], list[list[float]]],
        capacity: int = 400,
        crypto_key: Optional[bytes] = None,
        environment: str = "development",
        persist_path: Optional[Path] = None,
        crypto_key_path: Optional[Path] = None
    ):
        """Initialize Echo Veil memory layer.
        
        Args:
            embed_fn: Function to embed text into vectors (e.g., Ollama embed())
            capacity: Maximum active memories before decay kicks in
            crypto_key: AES-256-GCM key for production encryption, or None for dev
            environment: "development" or "production" - production requires valid crypto
            persist_path: Optional path to persist memory state; defaults to ~/.algo_cli/echo_veil_state.json
            crypto_key_path: Path to JSON file containing 'key_hex' field (loads if crypto_key is None)
        """
        if not ECHO_VEIL_AVAILABLE:
            raise ImportError(
                "Echo Veil not installed. Run: pip install echo-veil"
            )
        
        self.embed_fn = embed_fn
        self.capacity = capacity
        
        # Load crypto key from file if path provided and no key given
        if crypto_key is None and crypto_key_path is not None:
            import json as _json
            with open(crypto_key_path, 'r') as _f:
                _key_data = _json.load(_f)
            crypto_key = bytes.fromhex(_key_data['key_hex'])
        self.environment = environment
        self._loaded_state: dict[str, Any] | None = None
        self._memory_text_by_title: dict[str, str] = {}
        
        # Set up crypto shield for production
        if environment == "production":
            if crypto_key is None:
                raise ValueError(
                    "Production mode requires a crypto key. "
                    "Generate one with AesGcmCryptoShield.generate_key()"
                )
            self.shield = AesGcmCryptoShield(crypto_key)
        else:
            # Development uses null shield (no encryption)
            from echo_veil.crypto_shield import NullCryptoShield
            self.shield = NullCryptoShield()
        
        # Initialize Oracle with workspace config
        self.oracle = Oracle(WorkspaceConfig(capacity=capacity), shield=self.shield)
        
        # Persistence path
        if persist_path is None:
            self.persist_path = CONFIG_DIR / "echo_veil_state.json"
        else:
            self.persist_path = persist_path
        
        # Load state if exists
        self._load_state()
    
    def sprout(self, title: str, text: str, embedding: list[float]) -> None:
        """Add a new memory to the active tier.
        
        Args:
            title: Short descriptive title for the memory
            text: Full text content of the memory (stored in metadata)
            embedding: Vector representation from embed_fn
        """
        # Echo Veil uses topic as the vine identifier. The upstream API only
        # accepts topic + embedding, so retain text locally for diagnostics and
        # persistence metadata rather than silently discarding the argument.
        self._memory_text_by_title[str(title)] = str(text)
        self.oracle.sprout(title, embedding)
    
    def observe(self, query_embedding: list[float]) -> dict[str, Any]:
        """Process current intent through Echo Veil's decay model.
        
        Returns context about active/compressed/archived memories relevant to query.
        
        Args:
            query_embedding: Vector representation of the current query/intent
            
        Returns:
            Dict with keys:
                - active: List of currently active memory titles and proximity scores
                - compressed: Summary of recently compressed memories (if any)
                - drift_detected: Boolean indicating if intent has drifted significantly
        """
        # Run observe cycle which triggers decay
        self.oracle.observe(query_embedding)
        
        # Get current state from GardenersReport (counts only)
        gardeners_report = self.oracle.report()
        
        # Extract active memories directly from workspace vines
        workspace_vines = getattr(self.oracle.workspace, 'vines', []) or []
        active_memories = []
        for vine in workspace_vines:
            if hasattr(vine, 'topic'):
                title = str(vine.topic)
                active_memories.append({
                    "title": title,
                    "text": self._memory_text_by_title.get(title, ""),
                    "proximity": None,
                    "proximity_available": False,
                })
        
        # Extract compressed memories (twilight_grove may be a count or records)
        twilight_raw = getattr(gardeners_report, 'twilight_grove', 0)
        twilight_count = _tier_count(twilight_raw, _memory_records(twilight_raw))
        
        return {
            "active": active_memories,
            "compressed": f"{twilight_count} memories in twilight grove",
            "drift_detected": None,
            "drift_available": False,
        }
    
    def get_report(self) -> dict[str, Any]:
        """Get comprehensive memory state report.
        
        Returns:
            Dict with memory statistics and tier breakdown
        """
        gardeners_report = self.oracle.report()
        active_raw = getattr(gardeners_report, 'thriving_vines', [])
        compressed_raw = getattr(gardeners_report, 'twilight_grove', [])
        active_memories = _memory_records(active_raw)
        compressed_memories = _memory_records(compressed_raw)
        
        return {
            "active_memories": active_memories,
            "compressed_memories": compressed_memories,
            "archived_memories": [],  # Would need archive access
            "active_count": _tier_count(active_raw, active_memories),
            "compressed_count": _tier_count(compressed_raw, compressed_memories),
            "memory_pressure": getattr(gardeners_report, 'memory_pressure', None),
        }
    
    def capability_report(self) -> dict[str, Any]:
        """Get defensive readiness and capability report.
        
        Returns:
            Dict covering crypto, storage, vector index, persistence status
        """
        cap_report = self.oracle.capability_report()
        
        return {
            "crypto_status": getattr(cap_report, 'crypto_status', {}),
            "storage_status": getattr(cap_report, 'storage_status', {}),
            "vector_index_status": getattr(cap_report, 'vector_index_status', {})
        }
    
    def _save_state(self) -> None:
        """Persist memory state to disk."""
        try:
            # Extract serializable state from Oracle
            report = self.get_report()
            state = {
                "capacity": self.capacity,
                "environment": self.environment,
                "active_count": report.get("active_count", len(report.get("active_memories", []))),
                "compressed_count": report.get("compressed_count", len(report.get("compressed_memories", []))),
                "archived_count": len(report.get("archived_memories", [])),
                "memory_text_by_title": self._memory_text_by_title,
                "last_updated": datetime.now(timezone.utc).isoformat()
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(self.persist_path.parent, 0o700)
            _atomic_write_text(self.persist_path, json.dumps(state, indent=2))
            if os.name == "posix":
                os.chmod(self.persist_path, 0o600)
        except Exception as exc:
            # Don't fail on persistence errors - memory layer should be resilient.
            logger.debug("Echo Veil state save failed: %s", exc)
    
    def _load_state(self) -> None:
        """Load persisted state from disk if available."""
        try:
            if self.persist_path.exists():
                with open(self.persist_path, 'r') as f:
                    state = json.load(f)
                if isinstance(state, dict):
                    self._loaded_state = state
                    texts = state.get("memory_text_by_title")
                    if isinstance(texts, dict):
                        self._memory_text_by_title = {str(k): str(v) for k, v in texts.items()}
                # State is informational - Oracle manages actual memory tiers.
                return None
        except Exception as exc:
            logger.debug("Echo Veil state load failed: %s", exc)
    
    def __del__(self):
        """Cleanup - save state on deletion."""
        self._save_state()


def create_echo_veil_layer(
    embed_fn: Callable[[list[str]], list[list[float]]],
    config: dict[str, Any] | None = None,
    crypto_key_path: Optional[str] = None,
) -> Optional[EchoVeilMemoryLayer]:
    """Factory function to create Echo Veil layer from algo-cli config.
    
    Args:
        embed_fn: Embedding function (e.g., from harness.py)
        config: algo-cli config dict; if None, reads from ~/.algo_cli/config.json
        crypto_key_path: explicit path to key file (overrides config); the file
            may be either a JSON envelope containing a ``key_hex`` field or raw
            32-byte key material.
        
    Returns:
        EchoVeilMemoryLayer instance or None if not enabled/available
    """
    if not ECHO_VEIL_AVAILABLE:
        return None
    
    if config is None:
        config_path = CONFIG_DIR / "config.json"
        if config_path.exists():
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            return None
    
    # Check if Echo Veil is enabled in config
    if not config.get("echo_veil_enabled", False):
        return None
    
    capacity = config.get("echo_veil_capacity", 400)
    environment = "production" if config.get("echo_veil_production", False) else "development"
    
    # Resolve key path: explicit param wins, else fall back to config
    resolved_key_path = crypto_key_path or config.get("echo_veil_crypto_key_path")
    
    # Get crypto key (production mode only)
    crypto_key = None
    if environment == "production" and resolved_key_path:
        key_file = Path(resolved_key_path)
        if not key_file.exists():
            raise FileNotFoundError(f"Echo Veil crypto key not found: {resolved_key_path}")
        if key_file.is_symlink() or not key_file.is_file():
            raise PermissionError("Echo Veil crypto key must be a regular non-symlink file")
        if os.name == "posix" and stat.S_IMODE(key_file.stat().st_mode) & 0o077:
            raise PermissionError(
                "Echo Veil production key permissions are too broad; require mode 0600 or stricter"
            )
        
        # Try to parse as JSON envelope first, fall back to raw bytes
        try:
            with open(key_file, 'r') as f:
                key_data = json.load(f)
            key_hex = key_data.get("key_hex")
            if not key_hex:
                raise ValueError(f"Key file missing 'key_hex': {resolved_key_path}")
            crypto_key = bytes.fromhex(key_hex)
        except (json.JSONDecodeError, ValueError) as exc:
            # Not JSON or missing field; try raw bytes
            with open(key_file, 'rb') as f:
                raw = f.read()
            if len(raw) == 32:
                crypto_key = raw
            else:
                raise ValueError(
                    f"Key file at {resolved_key_path} is not a valid JSON envelope "
                    f"with 'key_hex' and is not exactly 32 raw bytes (got {len(raw)} bytes)"
                ) from exc
    
    return EchoVeilMemoryLayer(
        embed_fn=embed_fn,
        capacity=capacity,
        crypto_key=crypto_key,
        environment=environment,
    )
