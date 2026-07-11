"""B163, B164, B165, B175: Acrobat-derived runtime patterns.

- B163: Deterministic Variant / Experiment Bucketing
- B164: Broker / Sidecar Process Boundary
- B165: Chunked Web Resource Loading
- B175: Script Bytecode Compilation Cache
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
import hashlib
import time


# ── B163: Deterministic Variant / Experiment Bucketing ────────────────


@dataclass
class VariantRange:
    """A numeric range mapping to a variant."""
    start: int  # inclusive
    end: int    # inclusive
    variant: str


@dataclass
class ExperimentDefinition:
    """An experiment with variant ranges (B163)."""
    experiment_id: str
    ranges: list[VariantRange] = field(default_factory=list)
    control_variant: str = "control"

    def assign(self, subject_id: str) -> str:
        """Deterministically assign a variant based on subject_id hash."""
        bucket = self._hash_bucket(subject_id)
        for r in self.ranges:
            if r.start <= bucket <= r.end:
                return r.variant
        return self.control_variant

    def _hash_bucket(self, subject_id: str) -> int:
        """Stable hash to bucket 1-1000."""
        h = hashlib.sha256(f"{self.experiment_id}:{subject_id}".encode()).hexdigest()
        return int(h[:8], 16) % 1000 + 1


class ExperimentRegistry:
    """Registry of experiments (B163)."""

    def __init__(self) -> None:
        self._experiments: dict[str, ExperimentDefinition] = {}

    def register(self, experiment: ExperimentDefinition) -> None:
        self._experiments[experiment.experiment_id] = experiment

    def assign(self, experiment_id: str, subject_id: str) -> str:
        """Assign a variant for the given experiment and subject."""
        exp = self._experiments.get(experiment_id)
        if not exp:
            return "control"
        return exp.assign(subject_id)

    def all_experiments(self) -> list[str]:
        return list(self._experiments.keys())


# ── B164: Broker / Sidecar Process Boundary ───────────────────────────


@dataclass
class SidecarRequest:
    """A request to a sidecar worker (B164)."""
    command: str
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    permission_envelope: dict[str, bool] = field(default_factory=dict)


@dataclass
class SidecarResult:
    """Result from a sidecar worker (B164)."""
    success: bool = True
    output: Any = None
    error: str = ""
    timed_out: bool = False
    crashed: bool = False
    duration_ms: float = 0.0


class SidecarWorker(Protocol):
    """Protocol for sidecar workers (B164)."""
    def execute(self, request: SidecarRequest) -> SidecarResult: ...


class InProcessSidecar:
    """A simple in-process sidecar for testing (B164)."""

    def __init__(self, handlers: dict[str, Callable[[dict], Any]] | None = None) -> None:
        self._handlers = handlers or {}

    def register(self, command: str, handler: Callable[[dict], Any]) -> None:
        self._handlers[command] = handler

    def execute(self, request: SidecarRequest) -> SidecarResult:
        start = time.monotonic()
        handler = self._handlers.get(request.command)
        if not handler:
            return SidecarResult(
                success=False,
                error=f"unknown command: {request.command}",
                duration_ms=(time.monotonic() - start) * 1000,
            )
        # Check permission envelope
        if request.permission_envelope.get("external_send") and not request.permission_envelope.get("approved"):
            return SidecarResult(
                success=False,
                error="external_send not approved",
                duration_ms=(time.monotonic() - start) * 1000,
            )
        try:
            output = handler(request.params)
            return SidecarResult(
                success=True,
                output=output,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as e:
            return SidecarResult(
                success=False,
                error=str(e),
                crashed=True,
                duration_ms=(time.monotonic() - start) * 1000,
            )


class BrokerManager:
    """Manages broker/sidecar process boundaries (B164)."""

    def __init__(self) -> None:
        self._workers: dict[str, SidecarWorker] = {}

    def register_worker(self, name: str, worker: SidecarWorker) -> None:
        self._workers[name] = worker

    def dispatch(self, worker_name: str, request: SidecarRequest) -> SidecarResult:
        """Dispatch a request to a named worker."""
        worker = self._workers.get(worker_name)
        if not worker:
            return SidecarResult(success=False, error=f"unknown worker: {worker_name}")
        try:
            return worker.execute(request)
        except Exception as e:
            # Worker crash does not kill the broker
            return SidecarResult(success=False, error=str(e), crashed=True)

    def available_workers(self) -> list[str]:
        return list(self._workers.keys())


# ── B165: Chunked Web Resource Loading ──────────────────────────────


@dataclass
class ResourceChunk:
    """A chunk of a web resource (B165)."""
    chunk_id: str
    version: str = ""
    size_bytes: int = 0
    optional: bool = True


@dataclass
class ChunkLoadResult:
    """Result of loading a chunk."""
    chunk_id: str
    loaded: bool = True
    from_cache: bool = False
    error: str = ""
    load_time_ms: float = 0.0


class ChunkedResourceLoader:
    """Lazy-loads optional feature chunks (B165)."""

    def __init__(self) -> None:
        self._chunks: dict[str, ResourceChunk] = {}
        self._cache: dict[str, Any] = {}
        self._loaded: set[str] = set()

    def register_chunk(self, chunk: ResourceChunk) -> None:
        self._chunks[chunk.chunk_id] = chunk

    def load(self, chunk_id: str, loader: Callable[[], Any] | None = None) -> ChunkLoadResult:
        """Load a chunk by ID, using the provided loader function."""
        start = time.monotonic()
        chunk = self._chunks.get(chunk_id)
        if not chunk:
            return ChunkLoadResult(chunk_id=chunk_id, loaded=False, error=f"unknown chunk: {chunk_id}")
        if chunk_id in self._cache:
            return ChunkLoadResult(
                chunk_id=chunk_id, loaded=True, from_cache=True,
                load_time_ms=(time.monotonic() - start) * 1000,
            )
        if loader:
            try:
                self._cache[chunk_id] = loader()
                self._loaded.add(chunk_id)
                return ChunkLoadResult(
                    chunk_id=chunk_id, loaded=True,
                    load_time_ms=(time.monotonic() - start) * 1000,
                )
            except Exception as e:
                return ChunkLoadResult(
                    chunk_id=chunk_id, loaded=False, error=str(e),
                    load_time_ms=(time.monotonic() - start) * 1000,
                )
        return ChunkLoadResult(
            chunk_id=chunk_id, loaded=False, error="no loader provided",
            load_time_ms=(time.monotonic() - start) * 1000,
        )

    def is_loaded(self, chunk_id: str) -> bool:
        return chunk_id in self._loaded

    def invalidate(self, chunk_id: str) -> bool:
        """Invalidate a cached chunk."""
        if chunk_id in self._cache:
            del self._cache[chunk_id]
            self._loaded.discard(chunk_id)
            return True
        return False

    def all_chunks(self) -> list[str]:
        return list(self._chunks.keys())


# ── B175: Script Bytecode Compilation Cache ──────────────────────────


@dataclass
class CacheEntry:
    """A compiled script cache entry (B175)."""
    source_path: str
    cache_path: str
    source_mtime: float
    platform: str


class ScriptBytecodeCache:
    """Caches compiled scripts to avoid re-parsing on every startup (B175)."""

    def __init__(self, platform: str = "win") -> None:
        self._platform = platform
        self._entries: dict[str, CacheEntry] = {}
        self._compile_fn: Callable[[str], bytes] | None = None

    def set_compiler(self, compile_fn: Callable[[str], bytes]) -> None:
        self._compile_fn = compile_fn

    def get_or_compile(self, source_path: str, source_mtime: float) -> tuple[bytes, bool]:
        """Get cached bytecode or compile from source.
        Returns (bytecode, from_cache).
        """
        entry = self._entries.get(source_path)
        if entry and entry.source_mtime >= source_mtime and entry.platform == self._platform:
            # Cache hit
            return b"", True  # In real impl, would read cache file

        # Cache miss - compile
        if not self._compile_fn:
            raise RuntimeError("no compiler set")
        bytecode = self._compile_fn(source_path)
        self._entries[source_path] = CacheEntry(
            source_path=source_path,
            cache_path=f"{source_path}.cache",
            source_mtime=source_mtime,
            platform=self._platform,
        )
        return bytecode, False

    def invalidate(self, source_path: str) -> bool:
        """Invalidate a cache entry."""
        if source_path in self._entries:
            del self._entries[source_path]
            return True
        return False

    def is_stale(self, source_path: str, source_mtime: float) -> bool:
        """Check if a cache entry is stale."""
        entry = self._entries.get(source_path)
        if not entry:
            return True
        return entry.source_mtime < source_mtime or entry.platform != self._platform

    def cache_stats(self) -> dict[str, int]:
        """Return cache statistics."""
        return {
            "entries": len(self._entries),
            "platform": hash(self._platform),  # just for testing
        }