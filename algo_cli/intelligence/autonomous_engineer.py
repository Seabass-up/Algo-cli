#!/usr/bin/env python3
"""
Autonomous Engineer v1.0 — Foundation (Phase 1, hardened)

Single-file autonomous engineering runtime.

Core: Scheduler + Workers + Memory (SQLite) + Execution Sandbox + LLM Router

Hardening notes (see CHANGELOG block at bottom of file for the full list):
  - Fixed the shebang.
  - ExecutionSandbox now enforces a real timeout via subprocess isolation,
    with optional POSIX memory limits. This is *isolation*, not a security
    boundary — read the class docstring before running untrusted code.
  - MemoryEngine now applies WAL, enforces foreign keys, is thread-safe
    (lock-guarded writes, check_same_thread=False), and exposes close().
  - Scheduler now creates real session/task rows so the FK chain is valid.
  - Pruned unused imports.
"""

import ast
import json
import logging
import os
import re
import signal
import sqlite3
import tempfile
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# Optional deps
try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

# POSIX-only resource limits (available on Linux/WSL2, not on native Windows).
try:
    import resource  # type: ignore
    HAS_RESOURCE = True
except ImportError:
    resource = None  # type: ignore
    HAS_RESOURCE = False


# =============================================================================
# LOGGING
# =============================================================================
# Library pattern: attach a NullHandler so importing this module never emits
# output on its own. Call setup_logging() (or configure the root logger) to see
# messages. Diagnostics go through this logger; the CLI keeps actual *results*
# on stdout via print(), so they stay pipeable while chatter goes to stderr.
logger = logging.getLogger("autonomous_engineer")
logger.addHandler(logging.NullHandler())


def setup_logging(level: str = "INFO",
                  log_file: Optional[str] = None) -> logging.Logger:
    """Configure the package logger. Idempotent — safe to call repeatedly."""
    lg = logging.getLogger("autonomous_engineer")
    lg.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # Drop any non-Null handlers from a previous call to avoid duplicates.
    for h in list(lg.handlers):
        if not isinstance(h, logging.NullHandler):
            lg.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    stream = logging.StreamHandler()  # stderr
    stream.setFormatter(fmt)
    lg.addHandler(stream)
    if log_file:
        fileh = logging.FileHandler(log_file, encoding="utf-8")
        fileh.setFormatter(fmt)
        lg.addHandler(fileh)
    lg.propagate = False
    return lg


# =============================================================================
# CONFIGURATION
# =============================================================================
@dataclass
class Config:
    """Single source of truth for configuration."""
    db_path: str = "autonomous_engineer.db"
    workspace: str = "./workspace"
    default_model: str = "llama3.1"
    max_workers: int = 4
    default_timeout: float = 30.0
    benchmark_repeats: int = 5
    benchmark_warmup: int = 2
    sqlite_journal_mode: str = "WAL"
    sandbox_memory_limit_mb: Optional[int] = 512  # None = no limit
    require_correctness_test: bool = True
    benchmark_target_sample_time: float = 0.001
    benchmark_max_loops: int = 100_000
    os_sandbox_command: Optional[List[str]] = None

    def save(self, path: str = "config.json") -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        if Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(**data)
        return cls()


# =============================================================================
# MEMORY ENGINE (SQLite)
# =============================================================================
class MemoryEngine:
    """Persistent memory with SQLite. ACID, indexed, portable.

    Thread-safety: the connection is opened with check_same_thread=False and
    all writes are serialized through a single lock. This is the simplest
    correct model for the Phase-1 worker pool. If write throughput ever
    becomes the bottleneck, move to a connection-per-thread or a writer queue.
    """

    def __init__(self, db_path: str = "autonomous_engineer.db",
                 journal_mode: str = "WAL"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._transaction_rollback_only = False
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure(journal_mode)
        self._init_schema()

    def _configure(self, journal_mode: str) -> None:
        """Apply validated SQLite pragmas."""
        mode = str(journal_mode or "WAL").upper()
        valid_modes = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
        if mode not in valid_modes:
            raise ValueError(f"invalid sqlite_journal_mode: {journal_mode!r}")

        cursor = self.conn.cursor()
        # WAL improves concurrent read/write behavior; honored from Config.
        cursor.execute(f"PRAGMA journal_mode={mode};")
        # Referential integrity is OFF by default in SQLite — turn it on so the
        # session -> task -> attempt chain is actually enforced.
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        if mode == "WAL":
            cursor.execute("PRAGMA synchronous=NORMAL;")
        self.conn.commit()

    def _init_schema(self) -> None:
        """Core schema per spec (expandable)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                goal TEXT,
                strategy TEXT,
                status TEXT DEFAULT 'running'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                session_id INTEGER,
                name TEXT,
                status TEXT DEFAULT 'pending',
                dependencies TEXT,  -- JSON array
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY,
                task_id INTEGER,
                worker_name TEXT,
                code_snippet TEXT,
                success BOOLEAN,
                execution_time REAL,
                score_vector TEXT,  -- JSON {perf: 0.95, correctness: 1.0, ...}
                reflection TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS benchmarks (
                id INTEGER PRIMARY KEY,
                attempt_id INTEGER,
                median_time REAL,
                std_dev REAL,
                repeats INTEGER,
                confidence REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(attempt_id) REFERENCES attempts(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY,
                attempt_id INTEGER,
                what_happened TEXT,
                why TEXT,
                evidence TEXT,
                lessons TEXT,  -- JSON
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(attempt_id) REFERENCES attempts(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE,
                last_updated TEXT,
                git_commit TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY,
                repo_id INTEGER,
                file_path TEXT,
                name TEXT,
                type TEXT,  -- function, class, etc.
                calls TEXT,  -- JSON
                hash TEXT,
                FOREIGN KEY(repo_id) REFERENCES repositories(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                name TEXT PRIMARY KEY,
                priority INTEGER DEFAULT 100,
                capabilities TEXT  -- JSON
            )
        """)
        # Indexes on foreign keys / common lookup columns. Cheap to create, and
        # they matter as soon as the read path runs over a non-trivial history.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_attempts_worker ON attempts(worker_name)",
            "CREATE INDEX IF NOT EXISTS idx_bench_attempt ON benchmarks(attempt_id)",
            "CREATE INDEX IF NOT EXISTS idx_reflect_attempt ON reflections(attempt_id)",
            "CREATE INDEX IF NOT EXISTS idx_symbols_repo ON symbols(repo_id)",
            "CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)",
        ):
            cursor.execute(stmt)
        self.conn.commit()
        logger.info("[OK] MemoryEngine initialized: %s", self.db_path)

    def _commit_now(self) -> None:
        """Commit immediately; split out so tests can count real commits."""
        self.conn.commit()

    def _rollback_now(self) -> None:
        """Rollback immediately; split out so tests can count rollbacks."""
        self.conn.rollback()

    def _commit_if_needed(self) -> None:
        """Commit single writes, but defer inside MemoryEngine.batch()."""
        if self._transaction_depth == 0:
            self._commit_now()

    def batch(self) -> "_MemoryBatch":
        """Batch multiple writes into one SQLite transaction.

        Nested batches are safe: only the outermost context opens/closes the
        transaction. Any exception in a nested batch marks the outer transaction
        rollback-only so caught inner errors cannot accidentally commit partial
        state.
        """
        return _MemoryBatch(self)

    # ---- write helpers (all lock-guarded) ----

    def start_session(self, goal: str, strategy: Optional[str] = None) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO sessions (goal, strategy) VALUES (?, ?)",
                (goal, strategy),
            )
            self._commit_if_needed()
            return cursor.lastrowid

    def _dependencies_satisfied_locked(self, dependencies: List[int]) -> bool:
        """Return True only when every dependency task exists and completed."""
        if not dependencies:
            return True
        placeholders = ",".join("?" for _ in dependencies)
        cur = self.conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
            tuple(dependencies),
        )
        statuses = {int(row["id"]): row["status"] for row in cur.fetchall()}
        return all(statuses.get(int(dep)) == "completed" for dep in dependencies)

    def create_task(self, session_id: int, name: str,
                    dependencies: Optional[List[int]] = None) -> int:
        deps = [int(dep) for dep in (dependencies or [])]
        with self._lock:
            cursor = self.conn.cursor()
            status = (
                "running" if self._dependencies_satisfied_locked(deps)
                else "blocked"
            )
            cursor.execute(
                "INSERT INTO tasks (session_id, name, status, dependencies) "
                "VALUES (?, ?, ?, ?)",
                (session_id, name, status, json.dumps(deps)),
            )
            self._commit_if_needed()
            return cursor.lastrowid

    def log_attempt(self, task_id: int, worker: str, data: Dict) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO attempts
                    (task_id, worker_name, code_snippet, success,
                     execution_time, score_vector, reflection)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, worker, data.get("code"), data.get("success"),
                data.get("time"), json.dumps(data.get("score_vector", {})),
                data.get("reflection"),
            ))
            self._commit_if_needed()
            return cursor.lastrowid

    def log_benchmark(self, attempt_id: int, stats: Dict) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT INTO benchmarks
                    (attempt_id, median_time, std_dev, repeats, confidence)
                VALUES (?, ?, ?, ?, ?)
            """, (
                attempt_id, stats.get("median"), stats.get("stdev"),
                stats.get("repeats"), stats.get("confidence"),
            ))
            self._commit_if_needed()
            return cursor.lastrowid

    def register_worker(self, name: str, priority: int,
                        capabilities: List[str]) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO workers (name, priority, capabilities) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "priority=excluded.priority, "
                "capabilities=excluded.capabilities",
                (name, priority, json.dumps(capabilities)),
            )
            self._commit_if_needed()

    def complete_task(self, task_id: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE tasks SET status='completed' WHERE id = ?",
                (task_id,))
            self._commit_if_needed()

    def fail_task(self, task_id: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE tasks SET status='failed' WHERE id = ?",
                (task_id,))
            self._commit_if_needed()

    def complete_session(self, session_id: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE sessions SET status='completed' WHERE id = ?",
                (session_id,))
            self._commit_if_needed()

    def fail_session(self, session_id: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE sessions SET status='failed' WHERE id = ?",
                (session_id,))
            self._commit_if_needed()

    def block_session(self, session_id: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE sessions SET status='blocked' WHERE id = ?",
                (session_id,))
            self._commit_if_needed()

    # ---- read helpers (lock-guarded; single shared connection) ----
    # Reads are serialized through the same lock as writes because the
    # connection is shared across threads. WAL's reader/writer concurrency only
    # pays off with separate connections — the upgrade path if reads ever
    # become hot is a connection-per-thread pool.

    @staticmethod
    def _rows(cursor) -> List[Dict]:
        return [dict(r) for r in cursor.fetchall()]

    def get_session(self, session_id: int) -> Optional[Dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_task(self, task_id: int) -> Optional[Dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_tasks(self, session_id: int) -> List[Dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? ORDER BY id",
                (session_id,))
            return self._rows(cur)

    def get_attempts(self, task_id: int) -> List[Dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY id",
                (task_id,))
            return self._rows(cur)

    def best_attempt(self, task_id: int) -> Optional[Dict]:
        """Fastest SUCCESSFUL attempt for a task — the selection primitive.

        Orders by measured median time when a benchmark row exists, else by the
        attempt's own execution_time. Incorrect candidates are success=0 and so
        are excluded automatically.
        """
        with self._lock:
            cur = self.conn.execute("""
                SELECT a.*, b.median_time, b.confidence
                FROM attempts a
                LEFT JOIN benchmarks b ON b.attempt_id = a.id
                WHERE a.task_id = ? AND a.success = 1
                ORDER BY COALESCE(b.median_time, a.execution_time) ASC
                LIMIT 1
            """, (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def recent_attempts(self, limit: int = 10) -> List[Dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,))
            return self._rows(cur)

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class _MemoryBatch:
    """Re-entrant SQLite transaction scope for MemoryEngine writes."""

    def __init__(self, memory: MemoryEngine):
        self.memory = memory

    def __enter__(self) -> MemoryEngine:
        memory = self.memory
        memory._lock.acquire()
        if memory._transaction_depth == 0:
            memory.conn.execute("BEGIN")
            memory._transaction_rollback_only = False
        memory._transaction_depth += 1
        return memory

    def __exit__(self, exc_type, exc, tb) -> bool:
        memory = self.memory
        if exc_type is not None:
            memory._transaction_rollback_only = True
        memory._transaction_depth -= 1
        try:
            if memory._transaction_depth == 0:
                if memory._transaction_rollback_only:
                    memory._rollback_now()
                else:
                    memory._commit_now()
                memory._transaction_rollback_only = False
        finally:
            memory._lock.release()
        return False


# =============================================================================
# WORKER BASE
# =============================================================================
class Worker:
    """Base class for all pluggable workers."""
    NAME = "base"
    PRIORITY = 100
    CAPABILITIES: List[str] = []

    def __init__(self, memory: MemoryEngine, config: Config):
        self.memory = memory
        self.config = config

    def can_run(self, context: Dict) -> bool:
        """Determine if this worker is relevant."""
        return True

    def run(self, context: Dict) -> Dict:
        """Main execution. Must return an evidence-based result."""
        raise NotImplementedError("Worker must implement run()")


# =============================================================================
# WORKER: PerformanceWorker
# =============================================================================
class PerformanceWorker(Worker):
    """Benchmark candidate code with strict correctness gates.

    Safety model: the candidate process is isolated and timeout-limited, but it
    is not a security boundary. Parent-owned harness code controls timing and
    result writing; candidate and baseline code run in separate namespaces.
    """
    NAME = "performance"
    PRIORITY = 80
    CAPABILITIES = ["optimization", "benchmarking", "loop_transform"]

    def __init__(self, memory: MemoryEngine, config: Config):
        super().__init__(memory, config)
        self.sandbox = ExecutionSandbox(
            config.workspace,
            config.sandbox_memory_limit_mb,
            config.os_sandbox_command,
        )

    @staticmethod
    def _indent(text: str, spaces: int = 4) -> str:
        pad = " " * spaces
        return "\n".join(
            (pad + line) if line.strip() else line
            for line in text.splitlines()
        )

    def _build_harness(self, ctx: Dict) -> "tuple[Optional[str], Optional[str], Optional[str]]":
        """Build a single-candidate benchmark harness.

        Returns (script, error, result_file). Candidate code never receives the
        result-file path in its namespace. Candidate and baseline run in fresh
        setup-derived namespaces so baseline definitions cannot clobber the
        candidate under test.
        """
        call = ctx.get("call")
        if not call:
            return None, "no 'call' expression provided to time", None

        code = ctx.get("code") or ctx.get("initial_code") or ""
        setup = ctx.get("setup") or ""
        test = ctx.get("test")
        baseline_code = ctx.get("baseline_code") or ""
        baseline_call = ctx.get("baseline_call")
        warmup = max(0, int(self.config.benchmark_warmup))
        repeats = max(1, int(self.config.benchmark_repeats))
        target = max(0.0, float(self.config.benchmark_target_sample_time))
        max_loops = max(1, int(self.config.benchmark_max_loops))

        fd, result_file = tempfile.mkstemp(prefix="_ae_result_", suffix=".json")
        os.close(fd)

        p: List[str] = [
            "import builtins as _ae_builtins",
            "import json as _ae_json",
            "import time as _ae_time",
            "_ae_perf_counter = _ae_time.perf_counter",
            "_ae_open = _ae_builtins.open",
            f"_ae_result_file = {result_file!r}",
            f"_ae_setup_source = {setup!r}",
            f"_ae_code_source = {code!r}",
            f"_ae_test_source = {test!r}",
            f"_ae_call = {call!r}",
            f"_ae_baseline_source = {baseline_code!r}",
            f"_ae_baseline_call = {baseline_call!r}",
            f"_ae_warmup = {warmup}",
            f"_ae_repeats = {repeats}",
            f"_ae_target_sample_time = {target!r}",
            f"_ae_max_loops = {max_loops}",
            "",
            "def _ae_indent(_ae_text):",
            "    return '\\n'.join(('    ' + _ae_line) if _ae_line.strip() else _ae_line for _ae_line in _ae_text.splitlines())",
            "",
            "def _ae_build_ns(_ae_code_source, _ae_label):",
            "    _ae_ns = {'__builtins__': __builtins__}",
            "    if _ae_setup_source:",
            "        exec(compile(_ae_setup_source, '<setup>', 'exec'), _ae_ns)",
            "    if _ae_code_source:",
            "        exec(compile(_ae_code_source, _ae_label, 'exec'), _ae_ns)",
            "    return _ae_ns",
            "",
            "def _ae_make_bench(_ae_ns, _ae_expr, _ae_label):",
            "    _ae_src = 'def _ae_bench():\\n' + _ae_indent(_ae_expr)",
            "    exec(compile(_ae_src, _ae_label, 'exec'), _ae_ns)",
            "    return _ae_ns['_ae_bench']",
            "",
            "def _ae_calibrate(_ae_fn):",
            "    for _ in range(_ae_warmup):",
            "        _ae_fn()",
            "    _ae_loops = 1",
            "    while _ae_loops < _ae_max_loops:",
            "        _ae_t0 = _ae_perf_counter()",
            "        for _ in range(_ae_loops):",
            "            _ae_fn()",
            "        _ae_elapsed = _ae_perf_counter() - _ae_t0",
            "        if _ae_elapsed >= _ae_target_sample_time or _ae_target_sample_time <= 0:",
            "            break",
            "        _ae_loops = min(_ae_loops * 2, _ae_max_loops)",
            "    return max(1, _ae_loops)",
            "",
            "def _ae_measure(_ae_fn):",
            "    _ae_loops = _ae_calibrate(_ae_fn)",
            "    _ae_times = []",
            "    for _ in range(_ae_repeats):",
            "        _ae_t0 = _ae_perf_counter()",
            "        for _ in range(_ae_loops):",
            "            _ae_fn()",
            "        _ae_times.append((_ae_perf_counter() - _ae_t0) / _ae_loops)",
            "    return _ae_times, _ae_loops",
            "",
            "_ae_payload = {'times': [], 'correct': False, 'base_times': None, 'error': None}",
            "try:",
            "    _ae_candidate_ns = _ae_build_ns(_ae_code_source, '<candidate>')",
            "    _ae_correct = None",
            "    if _ae_test_source:",
            "        try:",
            "            exec(compile(_ae_test_source, '<test>', 'exec'), _ae_candidate_ns)",
            "            _ae_correct = True",
            "        except Exception:",
            "            _ae_correct = False",
            "    _ae_fn = _ae_make_bench(_ae_candidate_ns, _ae_call, '<bench-candidate>')",
            "    _ae_times, _ae_loops = _ae_measure(_ae_fn)",
            "    _ae_base_times = None",
            "    _ae_base_loops = None",
            "    if _ae_baseline_call:",
            "        _ae_base_ns = _ae_build_ns(_ae_baseline_source, '<baseline>')",
            "        _ae_base_fn = _ae_make_bench(_ae_base_ns, _ae_baseline_call, '<bench-baseline>')",
            "        _ae_base_times, _ae_base_loops = _ae_measure(_ae_base_fn)",
            "    _ae_payload = {",
            "        'times': _ae_times,",
            "        'correct': _ae_correct,",
            "        'base_times': _ae_base_times,",
            "        'loops_per_sample': _ae_loops,",
            "        'base_loops_per_sample': _ae_base_loops,",
            "        'error': None,",
            "    }",
            "except BaseException as _ae_exc:",
            "    _ae_payload = {'times': [], 'correct': False, 'base_times': None, 'error': repr(_ae_exc)}",
            "with _ae_open(_ae_result_file, 'w', encoding='utf-8') as _ae_f:",
            "    _ae_f.write(_ae_json.dumps(_ae_payload))",
        ]
        return "\n".join(p), None, result_file

    def _build_batch_harness(self, ctx: Dict,
                             candidates: List[str]) -> "tuple[str, Optional[str], Optional[str]]":
        """Build a parent-controlled batch benchmark harness."""
        call = ctx.get("call")
        if not call:
            return "", "no 'call' expression provided to time", None
        test = ctx.get("test")
        setup = ctx.get("setup") or ""
        warmup = max(0, int(self.config.benchmark_warmup))
        repeats = max(1, int(self.config.benchmark_repeats))
        target = max(0.0, float(self.config.benchmark_target_sample_time))
        max_loops = max(1, int(self.config.benchmark_max_loops))

        fd, result_file = tempfile.mkstemp(prefix="_ae_batch_", suffix=".json")
        os.close(fd)

        p: List[str] = [
            "import builtins as _ae_builtins",
            "import json as _ae_json",
            "import time as _ae_time",
            "_ae_perf_counter = _ae_time.perf_counter",
            "_ae_open = _ae_builtins.open",
            f"_ae_result_file = {result_file!r}",
            f"_ae_setup_source = {setup!r}",
            f"_ae_candidates = {candidates!r}",
            f"_ae_call = {call!r}",
            f"_ae_test_source = {test!r}",
            f"_ae_warmup = {warmup}",
            f"_ae_repeats = {repeats}",
            f"_ae_target_sample_time = {target!r}",
            f"_ae_max_loops = {max_loops}",
            "",
            "def _ae_indent(_ae_text):",
            "    return '\\n'.join(('    ' + _ae_line) if _ae_line.strip() else _ae_line for _ae_line in _ae_text.splitlines())",
            "",
            "def _ae_build_ns(_ae_code_source, _ae_label):",
            "    _ae_ns = {'__builtins__': __builtins__}",
            "    if _ae_setup_source:",
            "        exec(compile(_ae_setup_source, '<setup>', 'exec'), _ae_ns)",
            "    exec(compile(_ae_code_source, _ae_label, 'exec'), _ae_ns)",
            "    return _ae_ns",
            "",
            "def _ae_make_bench(_ae_ns):",
            "    _ae_src = 'def _ae_bench():\\n' + _ae_indent(_ae_call)",
            "    exec(compile(_ae_src, '<bench>', 'exec'), _ae_ns)",
            "    return _ae_ns['_ae_bench']",
            "",
            "def _ae_calibrate(_ae_fn):",
            "    for _ in range(_ae_warmup):",
            "        _ae_fn()",
            "    _ae_loops = 1",
            "    while _ae_loops < _ae_max_loops:",
            "        _ae_t0 = _ae_perf_counter()",
            "        for _ in range(_ae_loops):",
            "            _ae_fn()",
            "        _ae_elapsed = _ae_perf_counter() - _ae_t0",
            "        if _ae_elapsed >= _ae_target_sample_time or _ae_target_sample_time <= 0:",
            "            break",
            "        _ae_loops = min(_ae_loops * 2, _ae_max_loops)",
            "    return max(1, _ae_loops)",
            "",
            "def _ae_measure(_ae_fn):",
            "    _ae_loops = _ae_calibrate(_ae_fn)",
            "    _ae_times = []",
            "    for _ in range(_ae_repeats):",
            "        _ae_t0 = _ae_perf_counter()",
            "        for _ in range(_ae_loops):",
            "            _ae_fn()",
            "        _ae_times.append((_ae_perf_counter() - _ae_t0) / _ae_loops)",
            "    return _ae_times, _ae_loops",
            "",
            "_ae_all_results = {}",
            "for _ae_idx, _ae_code in enumerate(_ae_candidates):",
            "    try:",
            "        _ae_ns = _ae_build_ns(_ae_code, f'<candidate-{_ae_idx}>')",
            "        _ae_correct = None",
            "        if _ae_test_source:",
            "            try:",
            "                exec(compile(_ae_test_source, '<test>', 'exec'), _ae_ns)",
            "                _ae_correct = True",
            "            except Exception:",
            "                _ae_correct = False",
            "        _ae_fn = _ae_make_bench(_ae_ns)",
            "        _ae_times, _ae_loops = _ae_measure(_ae_fn)",
            "        _ae_all_results[_ae_idx] = {'times': _ae_times, 'correct': _ae_correct, 'loops_per_sample': _ae_loops, 'error': None}",
            "    except BaseException as _ae_exc:",
            "        _ae_all_results[_ae_idx] = {'times': [], 'correct': False, 'loops_per_sample': None, 'error': repr(_ae_exc)}",
            "",
            "with _ae_open(_ae_result_file, 'w', encoding='utf-8') as _ae_f:",
            "    _ae_f.write(_ae_json.dumps(_ae_all_results))",
        ]
        return "\n".join(p), None, result_file

    def _parse_bench(self, result_file: str) -> Optional[Dict]:
        """Read benchmark results from the temp JSON file."""
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                return json.loads(f.read())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _cleanup_result(result_file: Optional[str]) -> None:
        """Remove the temp result file if it exists."""
        if result_file:
            try:
                Path(result_file).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _summarize(times: List[float]) -> Dict[str, float]:
        n = len(times)
        median = statistics.median(times)
        stdev = statistics.stdev(times) if n >= 2 else 0.0
        rsd = (stdev / median) if median > 0 else 0.0
        return {
            "median": median,
            "mean": statistics.fmean(times),
            "stdev": stdev,
            "min": min(times),
            "repeats": n,
            "rsd": rsd,
            "confidence": max(0.0, 1.0 - rsd),
        }

    def _fail(self, ctx: Dict, reflection: str) -> Dict:
        result = {
            "success": False,
            "code": ctx.get("code") or ctx.get("initial_code"),
            "time": None,
            "score_vector": {},
            "reflection": reflection,
        }
        task_id = ctx.get("task_id")
        if task_id is not None:
            self.memory.log_attempt(task_id, self.NAME, result)
        return result

    @staticmethod
    def _describe(stats: Dict, correct, speedup) -> str:
        ms = stats["median"] * 1e3
        sd = stats["stdev"] * 1e3
        parts = [f"median {ms:.4f} ms over {stats['repeats']} runs (±{sd:.4f} ms)"]
        parts.append(
            "correctness not checked" if correct is None
            else ("correct" if correct else "INCORRECT (test failed)")
        )
        if speedup:
            parts.append(f"{speedup:.2f}x vs baseline")
        return "; ".join(parts)

    def _success_from_correctness(self, correct) -> bool:
        """Strict by default: unknown correctness is not success."""
        if self.config.require_correctness_test:
            return correct is True
        return correct is not False

    def run(self, context: Dict) -> Dict:
        script, err, result_file = self._build_harness(context)
        if err:
            return self._fail(context, f"harness error: {err}")

        exec_result = self.sandbox.run_code(
            script, timeout=self.config.default_timeout
        )
        if not exec_result.get("success"):
            reason = (exec_result.get("error")
                      or (exec_result.get("stderr") or "").strip()
                      or "unknown failure")
            self._cleanup_result(result_file)
            return self._fail(context, f"execution failed: {reason}")

        payload = self._parse_bench(result_file)
        self._cleanup_result(result_file)
        if not payload or not payload.get("times"):
            detail = payload.get("error") if isinstance(payload, dict) else None
            return self._fail(
                context,
                "no benchmark payload returned" + (f": {detail}" if detail else ""),
            )

        stats = self._summarize(payload["times"])
        correct = payload.get("correct")
        speedup = None
        base = payload.get("base_times")
        if base:
            base_stats = self._summarize(base)
            if stats["median"] > 0:
                speedup = base_stats["median"] / stats["median"]

        score_vector = {
            "median_s": round(stats["median"], 9),
            "mean_s": round(stats["mean"], 9),
            "stdev_s": round(stats["stdev"], 9),
            "min_s": round(stats["min"], 9),
            "repeats": stats["repeats"],
            "loops_per_sample": payload.get("loops_per_sample"),
            "correct": correct,
            "speedup_vs_baseline": round(speedup, 4) if speedup else None,
        }
        success = self._success_from_correctness(correct)
        reflection = self._describe(stats, correct, speedup)
        if correct is None and self.config.require_correctness_test:
            reflection += "; correctness not checked; refusing success"
        result = {
            "success": success,
            "code": context.get("code") or context.get("initial_code"),
            "time": stats["median"],
            "score_vector": score_vector,
            "reflection": reflection,
        }
        task_id = context.get("task_id")
        if task_id is not None:
            attempt_id = self.memory.log_attempt(task_id, self.NAME, result)
            if success:
                self.memory.log_benchmark(attempt_id, {
                    "median": stats["median"], "stdev": stats["stdev"],
                    "repeats": stats["repeats"],
                    "confidence": stats["confidence"],
                })
        return result

    def batch_run(self, context: Dict, candidates: List[str]) -> List[Dict]:
        """Benchmark multiple candidates in a single subprocess."""
        if not candidates:
            return []

        script, err, result_file = self._build_batch_harness(context, candidates)
        if err:
            return [self._fail(context, f"harness error: {err}")
                    for _ in candidates]

        exec_result = self.sandbox.run_code(
            script, timeout=self.config.default_timeout
        )
        if not exec_result.get("success"):
            reason = (exec_result.get("error")
                      or (exec_result.get("stderr") or "").strip()
                      or "unknown failure")
            self._cleanup_result(result_file)
            return [self._fail(context, f"execution failed: {reason}")
                    for _ in candidates]

        payload = self._parse_bench(result_file)
        self._cleanup_result(result_file)
        if not payload:
            return [self._fail(context, "no benchmark payload returned")
                    for _ in candidates]

        results = []
        task_id = context.get("task_id")
        batch_scope = self.memory.batch() if task_id is not None else None
        if batch_scope is not None:
            batch_scope.__enter__()
        try:
            for i, code in enumerate(candidates):
                entry = payload.get(str(i), {})
                times = entry.get("times", [])
                correct = entry.get("correct")
                if not times:
                    detail = entry.get("error")
                    results.append(self._fail(
                        {**context, "code": code},
                        f"candidate {i}: no times" + (f": {detail}" if detail else ""),
                    ))
                    continue

                stats = self._summarize(times)
                score_vector = {
                    "median_s": round(stats["median"], 9),
                    "mean_s": round(stats["mean"], 9),
                    "stdev_s": round(stats["stdev"], 9),
                    "min_s": round(stats["min"], 9),
                    "repeats": stats["repeats"],
                    "loops_per_sample": entry.get("loops_per_sample"),
                    "correct": correct,
                    "speedup_vs_baseline": None,
                }
                success = self._success_from_correctness(correct)
                reflection = self._describe(stats, correct, None)
                if correct is None and self.config.require_correctness_test:
                    reflection += "; correctness not checked; refusing success"
                result = {
                    "success": success,
                    "code": code,
                    "time": stats["median"],
                    "score_vector": score_vector,
                    "reflection": reflection,
                }
                if task_id is not None:
                    attempt_id = self.memory.log_attempt(task_id, self.NAME, result)
                    if success:
                        self.memory.log_benchmark(attempt_id, {
                            "median": stats["median"], "stdev": stats["stdev"],
                            "repeats": stats["repeats"],
                            "confidence": stats["confidence"],
                        })
                results.append(result)
        except Exception:
            if batch_scope is not None:
                batch_scope.__exit__(*sys.exc_info())
            raise
        else:
            if batch_scope is not None:
                batch_scope.__exit__(None, None, None)
        return results


# =============================================================================
# LLM ROUTER
# =============================================================================
class LLMRouter:
    """Provider-agnostic model router.

    For testability and offline use, an optional non-streaming `engine` and a
    streaming `stream_engine` can be injected. Each is a callable:
        engine(prompt: str, model: str) -> str
        stream_engine(prompt: str, model: str) -> Iterator[str]
    When neither is given, the ollama provider is used if available, else a
    stub. This mirrors the pluggable candidate_provider used by optimize_loop.
    """

    def __init__(self, config: Config, engine=None, stream_engine=None):
        self.config = config
        self._engine = engine
        self._stream_engine = stream_engine

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        model = model or self.config.default_model
        if self._engine is not None:
            return self._engine(prompt, model)
        if HAS_OLLAMA:
            try:
                resp = ollama.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp["message"]["content"]
            except Exception as e:  # noqa: BLE001 - router must not crash caller
                logger.warning("LLM call failed: %s", e)
                return f"[LLM Error: {e}]"
        return "[LLM Stub - install ollama]"

    def stream(self, prompt: str,
               model: Optional[str] = None) -> Iterator[str]:
        """Yield response text in chunks as they arrive.

        This is the path that lets a display update incrementally, so the
        time-to-first-display becomes the model's time-to-first-token rather
        than its full generation time.
        """
        model = model or self.config.default_model
        if self._stream_engine is not None:
            yield from self._stream_engine(prompt, model)
            return
        if HAS_OLLAMA:
            try:
                for part in ollama.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                ):
                    chunk = (part.get("message") or {}).get("content", "")
                    if chunk:
                        yield chunk
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM stream failed: %s", e)
                yield f"[LLM Error: {e}]"
            return
        yield "[LLM Stub - install ollama]"


# =============================================================================
# LATENCY PROBE (call -> display)
# =============================================================================
def make_simulated_stream(n_tokens: int = 30, ttft: float = 0.10,
                          per_token: float = 0.010, token: str = "lorem "):
    """Build a fake STREAMING engine that mimics a model: an initial think
    delay (time-to-first-token) then tokens at a steady rate. Offline demos /
    tests only -- real numbers come from a real model."""
    def _engine(prompt: str, model: str) -> Iterator[str]:
        time.sleep(ttft)
        for _ in range(n_tokens):
            time.sleep(per_token)
            yield token
    return _engine


def make_simulated_blocking(n_tokens: int = 30, ttft: float = 0.10,
                            per_token: float = 0.010, token: str = "lorem "):
    """Non-streaming counterpart with the SAME total cost as the streaming
    engine above, but it returns only once fully done. Pairing the two shows
    that streaming changes *when* output is visible, not total compute."""
    def _engine(prompt: str, model: str) -> str:
        time.sleep(ttft + n_tokens * per_token)
        return token * n_tokens
    return _engine


def measure_latency(router: "LLMRouter", prompt: str, *,
                    model: Optional[str] = None, repeats: int = 5,
                    stream: bool = True, sink=None) -> Dict[str, Any]:
    """Measure call->display latency over `repeats` runs.

    Reports median/stdev/min for:
      ttfd  - time to first displayed output (the perceived-latency metric).
      total - call -> fully displayed.
    In blocking mode nothing is visible until the whole response arrives, so
    ttfd == total by construction; that contrast is the point of the test.
    `sink(chunk)` is where display happens; default is a no-op for clean timing.
    """
    sink = sink or (lambda s: None)
    ttfd: List[float] = []
    total: List[float] = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        first = None
        if stream:
            for chunk in router.stream(prompt, model):
                if first is None:
                    first = time.perf_counter()
                sink(chunk)
        else:
            text = router.generate(prompt, model)
            first = time.perf_counter()  # nothing visible until full text ready
            sink(text)
        end = time.perf_counter()
        if first is None:
            first = end
        ttfd.append(first - t0)
        total.append(end - t0)

    def _stats(xs: List[float]) -> Dict[str, float]:
        n = len(xs)
        return {
            "median": statistics.median(xs),
            "stdev": statistics.stdev(xs) if n >= 2 else 0.0,
            "min": min(xs),
            "repeats": n,
        }

    return {
        "mode": "stream" if stream else "blocking",
        "ttfd": _stats(ttfd),
        "total": _stats(total),
    }


# =============================================================================
# EXECUTION SANDBOX
# =============================================================================
class ExecutionSandbox:
    """Process-isolated code execution with an enforced timeout.

    IMPORTANT — this is ISOLATION, not a SECURITY BOUNDARY. For model-generated
    code, configure ``Config.os_sandbox_command`` with an OS-level sandbox
    wrapper (container/VM/sandbox tool). This class enforces timeout, cleans up
    process trees, and optionally applies POSIX memory limits; it does not
    prevent deliberate filesystem/network access by itself.
    """

    def __init__(self, workspace: str = "./workspace",
                 memory_limit_mb: Optional[int] = 512,
                 os_sandbox_command: Optional[List[str]] = None):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.memory_limit_mb = memory_limit_mb
        if isinstance(os_sandbox_command, str):
            self.os_sandbox_command = [os_sandbox_command]
        else:
            self.os_sandbox_command = list(os_sandbox_command or [])

    def _preexec(self) -> None:
        """POSIX-only: cap address space so runaway allocations die fast."""
        if self.memory_limit_mb and HAS_RESOURCE:
            limit = self.memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    def _command_for_script(self, script: Path) -> List[str]:
        python_cmd = [sys.executable, str(script)]
        if not self.os_sandbox_command:
            return python_cmd
        mapping = {
            "{python}": sys.executable,
            "{script}": str(script),
            "{workspace}": str(self.workspace),
        }
        expanded = [mapping.get(part, part) for part in self.os_sandbox_command]
        if any(part in self.os_sandbox_command for part in ("{python}", "{script}")):
            return expanded
        return expanded + python_cmd

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, text=True, timeout=5,
                )
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass

    def run_code(self, code: str, timeout: float = 30.0) -> Dict[str, Any]:
        # Absolute path: subprocess cwd is the workspace, so a relative script
        # path would be resolved against it twice. resolve() avoids that.
        script = (self.workspace / f"_exec_{uuid.uuid4().hex}.py").resolve()
        script.write_text(code, encoding="utf-8")
        start = time.perf_counter()
        proc: Optional[subprocess.Popen] = None
        try:
            popen_kwargs: Dict[str, Any] = {
                "args": self._command_for_script(script),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "cwd": str(self.workspace),
            }
            if os.name == "posix":
                popen_kwargs["preexec_fn"] = self._preexec
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )

            proc = subprocess.Popen(**popen_kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                elapsed = time.perf_counter() - start
                return {
                    "success": False,
                    "time": elapsed,
                    "returncode": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "error": f"timeout after {timeout}s",
                }

            elapsed = time.perf_counter() - start
            return {
                "success": proc.returncode == 0,
                "time": elapsed,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except Exception as e:  # noqa: BLE001
            if proc is not None:
                self._kill_process_tree(proc)
            return {"success": False, "error": str(e)}
        finally:
            try:
                script.unlink(missing_ok=True)
            except OSError:
                pass


# =============================================================================
# OPTIMIZE SPEC
# =============================================================================
@dataclass
class OptimizeSpec:
    """Inputs for an optimization loop.

    Assumptions made explicit:
      - Every candidate must define the SAME callable that `call` invokes, so a
        single `call` and `test` apply to the reference and all variants.
      - `test` is REQUIRED: selecting an unverified candidate is unsafe, so the
        loop refuses to run without one.
    """
    name: str
    call: str
    reference_code: str = ""
    setup: str = ""
    test: str = ""
    n_variants: int = 3
    model: Optional[str] = None


# =============================================================================
# SCHEDULER (Core Kernel)
# =============================================================================
def _optimize_sequential_env_enabled() -> bool:
    """Return True when the legacy sequential optimize loop is requested."""
    raw = os.environ.get("ALGO_CLI_OPTIMIZE_SEQUENTIAL", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Scheduler:
    """Central operating system. Coordinates tasks and workers."""

    def __init__(self, config: Config):
        self.config = config
        self.memory = MemoryEngine(config.db_path, config.sqlite_journal_mode)
        self.router = LLMRouter(config)
        self.sandbox = ExecutionSandbox(
            config.workspace, config.sandbox_memory_limit_mb,
            config.os_sandbox_command,
        )
        self.workers: Dict[str, Worker] = {}
        self._discover_workers()
        self.active_session: Optional[int] = None

    @staticmethod
    def _iter_subclasses(cls) -> List[type]:
        """Recursively collect all subclasses (not just direct ones)."""
        found: List[type] = []
        for sub in cls.__subclasses__():
            found.append(sub)
            found.extend(Scheduler._iter_subclasses(sub))
        return found

    def _discover_workers(self) -> None:
        """Auto-register Worker subclasses (deduplicated, recursive).

        Each worker is also persisted to the `workers` table with its priority
        and capabilities, so routing decisions are introspectable from the DB.
        """
        with self.memory.batch():
            for worker_cls in dict.fromkeys(self._iter_subclasses(Worker)):
                w = worker_cls(self.memory, self.config)
                self.workers[w.NAME] = w
                self.memory.register_worker(w.NAME, w.PRIORITY, list(w.CAPABILITIES))
                logger.info("[OK] Registered worker: %s (priority=%d, caps=%s)",
                            w.NAME, w.PRIORITY, ",".join(w.CAPABILITIES) or "-")

    def start_session(self, goal: str, strategy: Optional[str] = None) -> int:
        self.active_session = self.memory.start_session(goal, strategy)
        return self.active_session

    def _select_worker(self, context: Dict) -> Optional[Worker]:
        """Pick a worker by precedence:

        1. context['worker']      — explicit name override.
        2. context['capability']  — any worker advertising that capability.
        3. otherwise              — any worker that can_run.

        Among capability/fallback candidates the lowest PRIORITY value wins
        (more specific workers use lower numbers); ties break by name for
        determinism. can_run() is always respected.
        """
        name = context.get("worker")
        if name:
            w = self.workers.get(name)
            return w if (w and w.can_run(context)) else None

        candidates = [w for w in self.workers.values() if w.can_run(context)]
        cap = context.get("capability")
        if cap:
            candidates = [w for w in candidates if cap in w.CAPABILITIES]
        if not candidates:
            return None
        candidates.sort(key=lambda w: (w.PRIORITY, w.NAME))
        return candidates[0]

    def run_task(self, task_name: str, context: Dict) -> Dict:
        """Create a task row, route to a worker by capability, and execute."""
        logger.info("[TASK] Running task: %s", task_name)
        if self.active_session is None:
            self.start_session(f"adhoc:{task_name}")

        dependencies = context.get("dependencies") or []
        task_id = self.memory.create_task(
            self.active_session, task_name, dependencies=dependencies
        )
        task = self.memory.get_task(task_id) or {}
        if task.get("status") == "blocked":
            return {
                "success": False,
                "status": "blocked",
                "task_id": task_id,
                "error": "task dependencies are not completed",
            }

        worker = self._select_worker(context)
        if worker is None:
            requested = (context.get("worker") or context.get("capability")
                         or "any")
            logger.warning("No suitable worker for '%s'", requested)
            self.memory.fail_task(task_id)
            return {"success": False,
                    "error": f"No suitable worker for '{requested}'"}

        logger.info("[ROUTE] routed to worker: %s", worker.NAME)
        result = worker.run({**context, "task_id": task_id})
        if result.get("success"):
            self.memory.complete_task(task_id)
        else:
            self.memory.fail_task(task_id)
        return result

    # ---- optimize loop (candidates -> benchmark -> select fastest correct) --

    def optimize_loop(self, spec: "OptimizeSpec",
                      candidate_provider=None,
                      *,
                      sequential: bool = False) -> Dict:
        """Generate variants, benchmark each, return the fastest CORRECT one.

        Flow:
          0. Benchmark the reference implementation. This guarantees a correct
             fallback: if no variant wins, the original is returned rather than
             a regression.
          1. Ask the provider (LLM by default) for candidate sources.
          2. Benchmark each valid, de-duplicated candidate against the reference.
          3. best_attempt() selects the fastest attempt with success=1 — the
             correctness gate has already excluded wrong-but-fast candidates.

        SECURITY: candidates are model-generated code executed in the sandbox,
        which is ISOLATION, not a security boundary, and strictly more exposed
        than running your own code. Do not point this at an untrusted model or
        host without an OS-level sandbox underneath.
        """
        if not sequential and not _optimize_sequential_env_enabled():
            return self.optimize_loop_batch(spec, candidate_provider=candidate_provider)

        return self.optimize_loop_sequential(spec, candidate_provider=candidate_provider)

    def optimize_loop_sequential(self, spec: "OptimizeSpec",
                                 candidate_provider=None) -> Dict:
        """Sequential optimize-loop implementation kept as an explicit fallback."""
        if not spec.call:
            return {"success": False, "error": "optimize_loop requires a 'call'"}
        if not spec.test:
            return {"success": False,
                    "error": "optimize_loop requires a 'test' (cannot select "
                             "an unverified candidate)"}

        worker = self._select_worker({"capability": "benchmarking"})
        if worker is None:
            return {"success": False, "error": "no benchmarking worker available"}

        if self.active_session is None:
            self.start_session(f"optimize:{spec.name}")
        task_id = self.memory.create_task(self.active_session, spec.name)

        base_ctx = {
            "task_id": task_id,
            "setup": spec.setup,
            "call": spec.call,
            "test": spec.test,
        }

        # 0. reference implementation (correct fallback + baseline timing)
        logger.info("optimize[%s]: benchmarking reference", spec.name)
        ref_res = worker.run({**base_ctx, "code": spec.reference_code})
        attempts0 = self.memory.get_attempts(task_id)
        original_id = attempts0[-1]["id"] if attempts0 else None
        original_median = (ref_res.get("score_vector") or {}).get("median_s")
        if not ref_res.get("success"):
            logger.warning("optimize[%s]: reference failed its own test (%s)",
                           spec.name, ref_res.get("reflection"))
            self.memory.fail_task(task_id)
            return {
                "success": False,
                "error": "reference implementation failed its own test",
                "reflection": ref_res.get("reflection"),
            }

        # 1. candidate sources
        provider = candidate_provider or self._llm_candidates
        try:
            raw = list(provider(spec))
        except Exception as e:  # noqa: BLE001
            logger.warning("optimize[%s]: candidate provider error: %s",
                           spec.name, e)
            raw = []
        logger.info("optimize[%s]: provider returned %d raw candidate(s)",
                    spec.name, len(raw))
        if raw:
            logger.warning("optimize[%s]: executing model-generated code in the "
                           "sandbox (isolation, NOT a security boundary)",
                           spec.name)
        if candidate_provider is None and raw and not self.config.os_sandbox_command:
            self.memory.fail_task(task_id)
            return {
                "success": False,
                "task_id": task_id,
                "error": ("model-generated candidates require "
                          "Config.os_sandbox_command for OS-level sandboxing"),
            }

        # 2. benchmark each valid, unique candidate against the reference
        seen = {self._norm(spec.reference_code)} if spec.reference_code else set()
        accepted = 0
        for i, item in enumerate(raw, 1):
            code = self._extract_code(item)
            if not code:
                logger.warning("optimize[%s]: candidate %d not valid Python; "
                               "skipped", spec.name, i)
                continue
            key = self._norm(code)
            if key in seen:
                logger.info("optimize[%s]: candidate %d duplicate; skipped",
                            spec.name, i)
                continue
            seen.add(key)
            accepted += 1
            # No baseline_code here: candidates are drop-in replacements that
            # share the reference's function name, so injecting the reference
            # into the same namespace would clobber the candidate (and silently
            # re-benchmark the reference). Speedup vs the original is computed by
            # the loop from stored medians instead.
            worker.run({**base_ctx, "code": code})

        # 3. select fastest correct attempt (reference + variants)
        winner = self.memory.best_attempt(task_id)
        all_attempts = self.memory.get_attempts(task_id)
        correct = sum(1 for a in all_attempts if a["success"])
        result: Dict[str, Any] = {
            "success": winner is not None,
            "task_id": task_id,
            "candidates_total": len(all_attempts),     # reference + variants
            "variants_accepted": accepted,
            "correct": correct,
            "winner": winner,
            "winner_is_original": bool(winner and winner["id"] == original_id),
            "original_median_s": original_median,
        }
        if winner is None:
            result["summary"] = ("no correct candidate (reference and all "
                                 "variants failed their test)")
            logger.warning("optimize[%s]: %s", spec.name, result["summary"])
            self.memory.fail_task(task_id)
            return result

        wmed = winner.get("median_time")
        speedup = (original_median / wmed) if (original_median and wmed) else None
        result["winner_median_s"] = wmed
        result["speedup_vs_original"] = round(speedup, 4) if speedup else None
        which = "original" if result["winner_is_original"] else "a variant"
        result["summary"] = (
            f"selected {which}: median {wmed * 1e3:.4f} ms"
            + (f"; {speedup:.2f}x vs original" if speedup else "")
            + f" ({correct}/{len(all_attempts)} correct)"
        )
        logger.info("optimize[%s]: %s", spec.name, result["summary"])
        self.memory.complete_task(task_id)
        return result

    # ---- batch optimize: all candidates in one subprocess ----

    def optimize_loop_batch(self, spec: "OptimizeSpec",
                             candidate_provider=None) -> Dict:
        """Optimized optimize_loop: benchmarks all candidates in a single
        subprocess (3x faster on Windows by eliminating N-1 interpreter
        startups).

        Same interface and return shape as optimize_loop, plus
        result['batch_mode'] = True.
        """
        if not spec.call:
            return {"success": False, "error": "optimize_loop requires a 'call'"}
        if not spec.test:
            return {"success": False,
                    "error": "optimize_loop requires a 'test'"}

        worker = self._select_worker({"capability": "benchmarking"})
        if worker is None:
            return {"success": False, "error": "no benchmarking worker available"}

        if self.active_session is None:
            self.start_session(f"optimize:{spec.name}")
        task_id = self.memory.create_task(self.active_session, spec.name)

        base_ctx = {
            "task_id": task_id,
            "setup": spec.setup,
            "call": spec.call,
            "test": spec.test,
        }

        # Collect all candidate codes (reference first)
        all_codes = [spec.reference_code]

        # Get variants from provider
        provider = candidate_provider or self._llm_candidates
        try:
            raw = list(provider(spec))
        except Exception as e:
            logger.warning("optimize[%s]: candidate provider error: %s",
                           spec.name, e)
            raw = []

        if candidate_provider is None and raw and not self.config.os_sandbox_command:
            self.memory.fail_task(task_id)
            return {
                "success": False,
                "task_id": task_id,
                "batch_mode": True,
                "error": ("model-generated candidates require "
                          "Config.os_sandbox_command for OS-level sandboxing"),
            }

        # Filter valid, unique candidates
        seen = {self._norm(spec.reference_code)} if spec.reference_code else set()
        accepted = 0
        for i, item in enumerate(raw, 1):
            code = self._extract_code(item)
            if not code:
                logger.warning("optimize[%s]: candidate %d not valid Python; skipped",
                               spec.name, i)
                continue
            key = self._norm(code)
            if key in seen:
                continue
            seen.add(key)
            accepted += 1
            all_codes.append(code)

        logger.info("optimize[%s]: batch benchmarking %d candidates in 1 subprocess",
                    spec.name, len(all_codes))

        # Benchmark ALL candidates in one subprocess
        results = worker.batch_run(base_ctx, all_codes)

        # Record original (reference) info
        original_median = ((results[0].get("score_vector") or {}).get("median_s")
                           if results else None)
        original_attempt_id = None
        attempts0 = self.memory.get_attempts(task_id)
        if attempts0:
            original_attempt_id = attempts0[0]["id"]

        # Check reference correctness
        if results and not results[0].get("success"):
            logger.warning("optimize[%s]: reference failed its own test (%s)",
                           spec.name, results[0].get("reflection"))
            self.memory.fail_task(task_id)
            return {
                "success": False,
                "error": "reference implementation failed its own test",
                "reflection": results[0].get("reflection"),
            }

        # Select fastest correct attempt
        winner = self.memory.best_attempt(task_id)
        all_attempts = self.memory.get_attempts(task_id)
        correct = sum(1 for a in all_attempts if a["success"])
        result: Dict[str, Any] = {
            "success": winner is not None,
            "task_id": task_id,
            "candidates_total": len(all_attempts),
            "variants_accepted": accepted,
            "correct": correct,
            "winner": winner,
            "winner_is_original": bool(winner and winner["id"] == original_attempt_id),
            "original_median_s": original_median,
            "batch_mode": True,
        }
        if winner is None:
            result["summary"] = ("no correct candidate (reference and all "
                                 "variants failed their test)")
            logger.warning("optimize[%s]: %s", spec.name, result["summary"])
            self.memory.fail_task(task_id)
            return result

        wmed = winner.get("median_time")
        speedup = (original_median / wmed) if (original_median and wmed) else None
        result["winner_median_s"] = wmed
        result["speedup_vs_original"] = round(speedup, 4) if speedup else None
        which = "original" if result["winner_is_original"] else "a variant"
        result["summary"] = (
            f"selected {which}: median {wmed * 1e3:.4f} ms"
            + (f"; {speedup:.2f}x vs original" if speedup else "")
            + f" ({correct}/{len(all_attempts)} correct)"
        )
        logger.info("optimize[%s]: %s", spec.name, result["summary"])
        self.memory.complete_task(task_id)
        return result

    def _llm_candidates(self, spec: "OptimizeSpec") -> List[str]:
        """Default provider: ask the LLM for n_variants drop-in replacements."""
        prompt = (
            "You are optimizing a Python function for speed.\n"
            "Return ONLY a single Python code block containing an optimized, "
            "drop-in replacement. It MUST define the same function name(s) and "
            "signature and be a correct, faithful replacement. No explanations, "
            "no tests, no examples — only the implementation.\n\n"
            "Reference implementation:\n```python\n"
            f"{spec.reference_code}\n```\n"
            f"It is invoked as: {spec.call}\n"
        )
        return [self.router.generate(prompt, spec.model)
                for _ in range(max(1, spec.n_variants))]

    @staticmethod
    def _extract_code(text: str) -> Optional[str]:
        """Pull a code block out of an LLM reply; return None if not valid
        Python (this is what rejects the ollama stub and prose-only replies)."""
        if not text:
            return None
        m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
        code = (m.group(1) if m else text).strip()
        if not code:
            return None
        try:
            ast.parse(code)
        except SyntaxError:
            return None
        return code

    @staticmethod
    def _norm(code: str) -> str:
        return "\n".join(ln.rstrip()
                         for ln in (code or "").strip().splitlines())

    def shutdown(self) -> None:
        if self.active_session is not None:
            tasks = self.memory.get_tasks(self.active_session)
            if any(t["status"] == "failed" for t in tasks):
                self.memory.fail_session(self.active_session)
            elif any(t["status"] == "blocked" for t in tasks):
                self.memory.block_session(self.active_session)
            else:
                self.memory.complete_session(self.active_session)
        self.memory.close()



def _demo_candidate_provider(spec: "OptimizeSpec") -> List[str]:
    """Built-in stand-in for the LLM, used when ollama is unavailable so the
    optimize-loop demo is runnable out of the box.

    Returns three variants of the demo `total` function: one faster + correct
    (builtin sum), one slower + correct (builds an intermediate list), and one
    INCORRECT (off-by-one) to show the correctness gate excluding it.
    """
    return [
        "def total(xs):\n    return sum(xs)",
        "def total(xs):\n    return sum([x for x in xs])",
        "def total(xs):\n    return sum(xs) + 1",  # wrong on purpose
    ]


# =============================================================================
# CLI / MAIN
# =============================================================================
def main() -> None:
    import argparse

    # Force UTF-8 on Windows consoles that default to cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # older Python without reconfigure

    parser = argparse.ArgumentParser(description="Autonomous Engineer v1.0")
    parser.add_argument(
        "command", choices=["optimize", "optimize-loop", "latency", "status"],
        default="optimize", nargs="?",
    )
    parser.add_argument("--target", default="matmul_demo")
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--variants", type=int, default=3,
                        help="candidates to generate in optimize-loop")
    parser.add_argument("--repeats", type=int, default=5,
                        help="timed runs per mode in latency")
    parser.add_argument("--log-level", default="INFO",
                        help="DEBUG, INFO, WARNING, ERROR (default: INFO)")
    parser.add_argument("--log-file", default=None,
                        help="optional path to also write logs to")
    parser.add_argument("--sequential", action="store_true",
                        help="use legacy sequential optimize-loop instead of batch mode")
    args = parser.parse_args()

    setup_logging(args.log_level, args.log_file)
    if args.command == "status":
        worker_names = sorted(cls.NAME for cls in Scheduler._iter_subclasses(Worker))
        print("Status: Kernel running. Workers:", worker_names)
        return

    config = Config(default_model=args.model)
    scheduler = Scheduler(config)

    try:
        if args.command == "optimize":
            scheduler.start_session(f"Optimize {args.target}")
            # Demo: replace a hand-rolled summation loop (baseline) with the
            # built-in sum (candidate) and measure the real speedup. Swap these
            # keys to benchmark your own code.
            context = {
                "setup": "data = list(range(10000))",
                "baseline_code": (
                    "def total(xs):\n"
                    "    s = 0\n"
                    "    for x in xs:\n"
                    "        s += x\n"
                    "    return s"
                ),
                "baseline_call": "total(data)",
                "call": "sum(data)",
                "test": "assert sum([1, 2, 3]) == 6",
                "capability": "optimization",  # route by capability, not name
                "target": args.target,
            }
            result = scheduler.run_task("performance_optimize", context)
            print("[OK] Optimization complete:", result)
        elif args.command == "optimize-loop":
            spec = OptimizeSpec(
                name="optimize_total",
                setup="data = list(range(10000))",
                reference_code=(
                    "def total(xs):\n"
                    "    s = 0\n"
                    "    for x in xs:\n"
                    "        s += x\n"
                    "    return s"
                ),
                call="total(data)",
                test="assert total([1, 2, 3]) == 6",
                n_variants=args.variants,
            )
            # Real LLM when ollama is present; deterministic demo otherwise.
            provider = None if HAS_OLLAMA else _demo_candidate_provider
            if provider is not None:
                logger.info("ollama unavailable; using built-in demo candidate "
                            "provider (stand-in for the LLM)")
            result = scheduler.optimize_loop(
                spec, candidate_provider=provider, sequential=args.sequential
            )
            print("[OK] Optimize-loop:", result.get("summary"))
            print("   winner_is_original:", result.get("winner_is_original"),
                  "| speedup_vs_original:", result.get("speedup_vs_original"))
        elif args.command == "latency":
            # Real model if ollama is present; a SIMULATED model otherwise so
            # the test runs offline (numbers then illustrate behavior only).
            simulated = not HAS_OLLAMA
            if simulated:
                logger.info("ollama unavailable; using a SIMULATED model "
                            "(numbers illustrate behavior, not a real model)")
                router = LLMRouter(
                    config,
                    engine=make_simulated_blocking(),
                    stream_engine=make_simulated_stream(),
                )
            else:
                router = LLMRouter(config)

            prompt = ("Write one short paragraph on why streaming reduces "
                      "perceived latency.")

            # 1) one visible streamed run so the display can be seen updating
            print("--- streamed display (tokens appear as they arrive) ---")
            t0 = time.perf_counter()
            first = None
            for chunk in router.stream(prompt, args.model):
                if first is None:
                    first = time.perf_counter()
                sys.stdout.write(chunk)
                sys.stdout.flush()
            print()
            if first is not None:
                print("(first token visible after %.1f ms)"
                      % ((first - t0) * 1e3))

            # 2) timed comparison with a no-op sink (clean numbers)
            s = measure_latency(router, prompt, model=args.model,
                                repeats=args.repeats, stream=True)
            b = measure_latency(router, prompt, model=args.model,
                                repeats=args.repeats, stream=False)
            tag = " (simulated)" if simulated else ""
            print("\n--- call->display latency, %d runs%s ---"
                  % (args.repeats, tag))
            print("  streaming  TTFD median: %8.1f ms | total median: %8.1f ms"
                  % (s["ttfd"]["median"] * 1e3, s["total"]["median"] * 1e3))
            print("  blocking   TTFD median: %8.1f ms | total median: %8.1f ms"
                  % (b["ttfd"]["median"] * 1e3, b["total"]["median"] * 1e3))
            if s["ttfd"]["median"] > 0:
                ratio = b["ttfd"]["median"] / s["ttfd"]["median"]
                print("  -> streaming shows first output %.1fx sooner "
                      "(total compute ~unchanged)" % ratio)
        else:
            print("Status: Kernel running. Workers:",
                  list(scheduler.workers.keys()))
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()


# =============================================================================
# CHANGELOG (hardening pass)
# =============================================================================
# 1.  Shebang fixed: "!/usr/bin/env python3" -> "#!/usr/bin/env python3".
# 2.  ExecutionSandbox: enforces timeout via subprocess; optional POSIX memory
#     cap; captures stdout/stderr/returncode; cleans up temp scripts. Now an
#     honest isolation boundary (documented as NOT a security boundary).
# 3.  MemoryEngine: applies PRAGMA journal_mode (WAL) from Config; enables
#     PRAGMA foreign_keys=ON; check_same_thread=False with a write lock;
#     added start_session/create_task/close helpers.
# 4.  Scheduler: creates real session + task rows so the FK chain validates;
#     wires Config.workspace / timeout / memory limit into the sandbox;
#     recursive, deduplicated worker discovery; guarded main() with try/finally.
# 5.  Pruned unused imports (ast, random, traceback, datetime, numpy,
#     importlib.util, field, Type, Callable, Set).
#
# Deferred (recommended, not done to limit scope):
#   - Replace print() with the logging module for level control + file output.
#   - Add query helpers + indexes to MemoryEngine for the read path.
#   - Capability-based routing instead of name fallback.
#
# Phase-2 update — PerformanceWorker now executes + benchmarks for real:
#   - Builds a single measurement harness, runs it once in the sandbox (so
#     interpreter startup is paid once, not per repeat), times each call with
#     perf_counter, and parses raw timings off a sentinel stdout line.
#   - Computes median/mean/stdev/min; persists a benchmarks row per attempt.
#   - Optional `test` => real correctness (True/False), else None ("not
#     checked"). Optional baseline => measured speedup. No fabricated scores.
#   - MemoryEngine.log_benchmark added; Config gained benchmark_warmup.
#
# Known limits of the benchmark path (by design, documented for honesty):
#   - Wall-clock on one machine; default_timeout covers the WHOLE harness, so a
#     too-low timeout yields no data rather than partial data.
#   - 'confidence' = 1 - relative stdev (measurement stability), NOT a
#     statistical CI. Default 5 repeats is low; raise benchmark_repeats.
#   - Harness injects caller-provided code as text; safe only because the
#     sandbox is isolation, NOT a security boundary (see ExecutionSandbox).
#
# Phase-2.1 update — cleared the deferred list:
#   - Logging: print() replaced by a package logger (NullHandler by default;
#     setup_logging() wires stderr + optional file). CLI gains --log-level /
#     --log-file. Diagnostics -> logger (stderr); results stay on stdout.
#   - Read path: indexes on FK/lookup columns; query helpers get_session,
#     get_tasks, get_attempts, recent_attempts, and best_attempt() (fastest
#     SUCCESSFUL attempt — the selection primitive for a future optimize loop).
#   - Routing: capability-based via Scheduler._select_worker (explicit name >
#     capability > can_run fallback; lowest PRIORITY wins, name breaks ties).
#     Workers are now persisted to the `workers` table on discovery, so routing
#     inputs are introspectable instead of the table being dead.
#
# Phase-2.2 update — the optimize loop is wired up:
#   - Scheduler.optimize_loop(spec, candidate_provider=None): benchmarks the
#     reference (candidate 0, the correct fallback), generates variants via the
#     provider (LLMRouter by default; pluggable for testing / no-ollama use),
#     benchmarks each valid+unique one vs the reference, then best_attempt()
#     selects the fastest CORRECT result. Wrong candidates are excluded by the
#     correctness gate; if nothing beats the original, the original is returned.
#   - _extract_code (ast-validated, fence-aware) rejects prose/stub replies;
#     _norm de-dupes; candidates routed via capability ("benchmarking").
#   - CLI: `optimize-loop` command (+ --variants); OptimizeSpec dataclass with a
#     required `test`. _demo_candidate_provider stands in for the LLM offline.
#   - SECURITY: this runs MODEL-GENERATED code in the sandbox — isolation, not a
#     security boundary, and more exposed than running your own code.
#
# Phase-2.3 update — call->display latency probe + streaming:
#   - LLMRouter gained stream() (ollama stream=True) plus injectable `engine` /
#     `stream_engine` for offline testing, mirroring the candidate_provider
#     pattern. generate() is unchanged for existing callers.
#   - measure_latency() reports TTFD (time to first displayed output) and total
#     call->display, median/stdev/min, for streaming vs blocking. TTFD is the
#     perceived-latency metric; streaming collapses it to time-to-first-token
#     while total compute is unchanged.
#   - make_simulated_stream / make_simulated_blocking: matched-cost fakes so the
#     `latency` CLI command runs offline; real numbers come from a real model.
#   - CLI: `latency` command (+ --repeats). All new output is ASCII to match the
#     console encoding fix.
#
# Phase-3 update — harness integration + hardening (algo_cli):
#   - Copied to algo_cli/intelligence/autonomous_engineer.py (clean module name).
#   - Exported from algo_cli/intelligence/__init__.py as AEConfig, MemoryEngine,
#     ExecutionSandbox, PerformanceWorker, LLMRouter, AEScheduler, OptimizeSpec.
#   - Fixed: emoji -> ASCII tags ([OK], [TASK], [ROUTE]) + UTF-8 reconfigure.
#   - Fixed: optimize_loop hard-fails when reference fails its own test.
#   - Fixed: task/session status transitions (complete_task, fail_task,
#     complete_session) at all exit paths.
#   - Fixed: benchmark harness uses _ae_ prefixed internals + temp JSON file
#     instead of stdout sentinel (prevents spoofing by model-generated code).
#   - Fixed: Config save/load uses encoding="utf-8".
#   - Fixed: result file path uses .resolve() for subprocess cwd compatibility.
#   - Added: 17 tests in tests/test_autonomous_engineer.py.
#   - Added: 10 empirical algorithm benchmarks (B178-B187) in ALGO.md.