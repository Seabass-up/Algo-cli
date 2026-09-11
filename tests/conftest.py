"""Shared pytest fixtures for Algo CLI.

CONFIG_DIR is resolved at import time inside the package, so the test config
directory is registered via ALGO_CLI_CONFIG_DIR here — before any algo_cli
module is imported by a test. Each test then gets a clean directory and reset
module-level caches via the autouse `clean_state` fixture.
"""

from __future__ import annotations

import importlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Script tests need the checkout root, while installed-runtime tests must keep
# site-packages ahead of source in a non-editable qualification environment.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

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


def pytest_sessionstart(session: pytest.Session) -> None:
    """CI must exercise ripgrep as well as the deliberately forced fallback."""
    if os.environ.get("ALGO_TEST_REQUIRE_RIPGREP") != "1":
        return
    executable = shutil.which("rg")
    if executable is None:
        raise pytest.UsageError("CI requires ripgrep 15.2.0; missing backend coverage is not a pass")
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise pytest.UsageError("CI ripgrep preflight failed") from exc
    if completed.stdout.splitlines()[:1] != [b"ripgrep 15.2.0"]:
        raise pytest.UsageError("CI requires the pinned ripgrep 15.2.0 backend")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reject oversized test metadata before verbose reporting can flood CI."""
    oversized = [
        (item, len(item.nodeid.encode("utf-8", errors="backslashreplace")))
        for item in items
        if len(item.nodeid.encode("utf-8", errors="backslashreplace")) > 1024
    ]
    if oversized:
        examples = "; ".join(
            f"{ascii(item.nodeid.partition('[')[0])[:120]} ({size} bytes)" for item, size in oversized[:5]
        )
        raise pytest.UsageError(
            f"{len(oversized)} test IDs exceed 1024 bytes. Use short explicit pytest.param ids "
            f"without changing the fixture payloads. Examples: {examples}"
        )


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
        harness._PROTECTED_MEMORY_AUTHORITY = False
        harness._QUERY_VEC_CACHE.clear()
        harness._BM25_INDEX_CACHE.clear()
        harness._VECTOR_MATRIX_CACHE.clear()
    except ImportError:
        pass
    try:
        from ollama_cli import model_info

        model_info._CACHE.clear()
    except ImportError:
        pass
    try:
        from algo_cli import memory_echo_veil

        if not hasattr(memory_echo_veil, "reset_echo_veil_layer"):
            memory_echo_veil = importlib.reload(memory_echo_veil)
        memory_echo_veil.reset_echo_veil_layer()
    except (ImportError, RuntimeError):
        pass

    yield

    try:
        from algo_cli import memory_echo_veil

        if not hasattr(memory_echo_veil, "reset_echo_veil_layer"):
            memory_echo_veil = importlib.reload(memory_echo_veil)
        memory_echo_veil.reset_echo_veil_layer()
    except (ImportError, RuntimeError):
        pass

    shutil.rmtree(_TEST_CONFIG_DIR, ignore_errors=True)


@pytest.fixture
def config_dir() -> Path:
    return _TEST_CONFIG_DIR


@pytest.fixture(params=("utf-8", "cp1252"), ids=("utf8-default", "windows-cp1252-default"))
def text_default_encoding(monkeypatch, request):
    """Exercise opted-in text readers under both common host defaults."""
    original = Path.read_text

    def read_text(path, encoding=None, errors=None, **kwargs):
        return original(path, encoding=request.param if encoding is None else encoding, errors=errors, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return request.param


_KEYWORDS = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "harness",
    "skill",
    "lesson",
    "footer",
    "embed",
    "rust",
    "python",
    "config",
    "tool",
    "index",
    "cosine",
    "model",
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
