"""Short-lived routing metadata, never evidence, inference or authorization."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any

from . import decision_layer as d


@dataclass
class ReadinessCache:
    entries: OrderedDict[str, tuple[float, dict]] = field(default_factory=OrderedDict)
    lock: Any = field(default_factory=threading.Lock)

    def __deepcopy__(self, memo: dict) -> ReadinessCache:
        result = ReadinessCache()
        memo[id(self)] = result
        return result

    def get(self, key: str, ttl: float) -> dict | None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            age = time.monotonic() - entry[0]
            if not 0 <= age < ttl:
                del self.entries[key]
                return None
            self.entries.move_to_end(key)
            result = deepcopy(entry[1])
            result["readiness_cache"] = {"hit": True, "age_ms": round(age * 1000, 3)}
            return result

    def put(self, key: str, status: dict) -> None:
        with self.lock:
            self.entries[key] = (time.monotonic(), deepcopy(status))
            self.entries.move_to_end(key)
            while len(self.entries) > 16:
                self.entries.popitem(last=False)


def cache_for(cfg: Any) -> ReadinessCache:
    cache = getattr(cfg, "_decision_readiness_cache", None)
    if not isinstance(cache, ReadinessCache):
        cache = ReadinessCache()
        cfg._decision_readiness_cache = cache
    return cache


def status_for_routing(cfg: Any, provider: Any, snapshots: dict) -> dict:
    if isinstance(provider, d.OllamaProvider):
        # This snapshot exists only for this catalog. Installed model digests and
        # loading state are rediscovered on the next operation, never TTL-cached.
        return provider._inspect(snapshots) if provider.local else provider.status()
    if not isinstance(provider, (d.JevProvider, d.CompanionProvider)):
        return provider.status()
    ttl = d._number(getattr(cfg, "decision_readiness_ttl_seconds", 5), 0, 30)
    path = Path(cfg.jev_kernel_cli) if isinstance(provider, d.JevProvider) else provider._path()
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    key = d.digest({"adapter": type(provider).__name__, "model": provider.model,
        "configured_path": cfg.jev_kernel_cli if isinstance(provider, d.JevProvider) else provider.cli,
        "path": str(resolved), "identity": [stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size,
                                            stat.st_mode, stat.st_uid, stat.st_gid],
        "enabled": getattr(cfg, "jev_kernel_enabled", False),
        "settings": getattr(cfg, "decision_provider_settings", {}).get(provider.name, {})})
    cache = cache_for(cfg)
    cached = cache.get(key, ttl) if ttl else None
    if cached is not None:
        return cached
    status = provider.status()
    if (ttl and type(status) is dict
            and not any(status.get(key) is False for key in ("ok", "ready", "available", "enabled"))):
        cache.put(key, status)
    if type(status) is dict:
        status = {**status, "readiness_cache": {"hit": False, "age_ms": 0}}
    return status
