"""Shared pytest fixtures for Algo CLI.

CONFIG_DIR is resolved at import time inside the package, so the test config
directory is registered via ALGO_CLI_CONFIG_DIR here — before any algo_cli
module is imported by a test. Each test then gets a clean directory and reset
module-level caches via the autouse `clean_state` fixture.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
from pathlib import Path

import pytest

# Must run before any `import algo_cli.*` / `ollama_cli.*` in the test files.
_TEST_CONFIG_DIR = Path(tempfile.gettempdir()) / f"algo_cli_pytest_{os.getpid()}"
# ALGO_CLI_CONFIG_DIR takes precedence over OLLAMA_CLI_CONFIG_DIR in config resolution.
os.environ["ALGO_CLI_CONFIG_DIR"] = str(_TEST_CONFIG_DIR)
os.environ["OLLAMA_CLI_CONFIG_DIR"] = str(_TEST_CONFIG_DIR)
os.environ["ALGO_CLI_DISABLE_WINDOWS_HOME_FALLBACK"] = "1"


def _repoint_package_config_dirs(target: Path) -> None:
    """CONFIG_DIR/INDEX_PATH are bound at import time; repoint for isolated tests."""
    import algo_cli.config as config_module
    import algo_cli.harness as harness_module
    import algo_cli.identity as identity_module

    config_module.CONFIG_DIR = target
    config_module.CONFIG_FILE = target / "config.json"
    config_module.MEMORY_FILE = target / "memory.json"
    config_module.MEMORY_CANDIDATE_STATE_FILE = target / "memory_candidate_state.json"
    config_module.HISTORY_DIR = target / "saves"
    config_module.CONTEXT_ARCHIVE_DIR = target / "context_archives"
    config_module.PROMPT_HISTORY_FILE = target / "prompt_history.txt"
    config_module.PERF_HISTORY_FILE = target / "perf_history.jsonl"
    config_module.EMBED_PERF_FILE = target / "embed_perf.jsonl"
    config_module.DEFAULT_RUNTIME_ENV_FILE = target / "env"
    config_module.DOTENV_RUNTIME_ENV_FILE = target / ".env"

    harness_module.INDEX_PATH = target / "harness_index.json"
    harness_module.EXTRA_ROOTS_PATH = target / "harness_roots.json"

    identity_module.IDENTITY_DIR = target / "identity"
    identity_module.LESSONS_INDEX_PATH = identity_module.IDENTITY_DIR / "lessons_index.json"


_repoint_package_config_dirs(_TEST_CONFIG_DIR)

# Avoid full harness rebuilds during unit tests (real SOURCE_ROOTS → 800+ records).
try:
    from ollama_cli import harness as _harness_bootstrap

    _harness_bootstrap.SOURCE_ROOTS = ()
    _harness_bootstrap.load_extra_source_roots = lambda: []
    _harness_bootstrap.find_rust_indexer = lambda: None
except ImportError:
    pass


@pytest.fixture(autouse=True)
def clean_state():
    """Wipe the test config dir and reset module-level caches around every test."""
    shutil.rmtree(_TEST_CONFIG_DIR, ignore_errors=True)
    _TEST_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _repoint_package_config_dirs(_TEST_CONFIG_DIR)

    try:
        from ollama_cli import identity

        identity._CACHE.clear()
        identity._LESSONS_INDEX = None
        identity._QUERY_VEC_CACHE.clear()
    except ImportError:
        pass
    try:
        from ollama_cli import harness

        harness.SOURCE_ROOTS = ()
        harness.load_extra_source_roots = lambda: []
        harness.find_rust_indexer = lambda: None
        harness._INDEX_CACHE = None
        harness._INDEX_CACHE_SIGNATURE = None
        harness._STALE_CHECK_CACHE = None
        harness._ID_LOOKUP = None
        harness._extra_roots_cache = None
        harness._QUERY_VEC_CACHE.clear()
    except ImportError:
        pass
    try:
        from ollama_cli import model_info

        model_info._CACHE.clear()
    except ImportError:
        pass

    yield

    shutil.rmtree(_TEST_CONFIG_DIR, ignore_errors=True)


@pytest.fixture
def config_dir() -> Path:
    return _TEST_CONFIG_DIR


_KEYWORDS = [
    "alpha", "beta", "gamma", "delta", "harness", "skill", "lesson",
    "footer", "embed", "rust", "python", "config", "tool", "index", "cosine", "model",
]


def make_fake_embed(dims: int = 16):
    """Deterministic, keyword-biased embedder for retrieval tests (no network)."""

    def _embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            rng = random.Random(hash(text) & 0xFFFFFFFF)
            vec = [rng.random() * 0.05 for _ in range(dims)]
            low = (text or "").lower()
            for i, keyword in enumerate(_KEYWORDS[:dims]):
                if keyword in low:
                    vec[i] = 1.0
            out.append(vec)
        return out

    return _embed
