from __future__ import annotations

import json
import os

import pytest

from algo_cli import config, context_budget, identity
from algo_cli import julia_memory_candidates as memory_candidates
from algo_cli import julia_memory_runtime as memory_runtime
from algo_cli import main
from algo_cli.config import Config


def _write_memory(facts: list[str]) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text(json.dumps(facts), encoding="utf-8")


# --- memory.json that cannot be read must never be rewritten as [] ---


def test_corrupt_memory_file_blocks_remember_instead_of_wiping_it() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    original = '["first fact", "second fact",'
    config.MEMORY_FILE.write_text(original, encoding="utf-8")
    cfg = Config.load()
    assert cfg.memory_load_error == "unreadable"

    with pytest.raises(memory_runtime.MemorySystemError, match="no memory was changed"):
        memory_runtime.remember_fact(cfg, "the build tool is uv")

    assert config.MEMORY_FILE.read_text(encoding="utf-8") == original
    assert memory_runtime.MemoryCatalog().records() == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_group_writable_memory_file_is_reported_and_preserved() -> None:
    _write_memory(["first fact", "second fact"])
    os.chmod(config.MEMORY_FILE, 0o664)
    cfg = Config.load()

    assert cfg.memories == []
    assert cfg.memory_load_error == "unreadable"
    with pytest.raises(config.MemoryFileUnreadableError):
        cfg.remember_fact("the build tool is uv")
    with pytest.raises(config.MemoryFileUnreadableError):
        cfg.forget_memory_index(0)
    with pytest.raises(config.MemoryFileUnreadableError):
        cfg.reconcile_memory_facts(additions=["x fact"])
    os.chmod(config.MEMORY_FILE, 0o600)
    assert json.loads(config.MEMORY_FILE.read_text(encoding="utf-8")) == ["first fact", "second fact"]


def test_non_list_memory_file_is_not_replaced() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"facts": ["keep me"]}', encoding="utf-8")
    cfg = Config.load()
    assert cfg.memory_load_error == "not_a_list"

    with pytest.raises(config.MemoryFileUnreadableError):
        cfg.remember_fact("the build tool is uv")
    assert "keep me" in config.MEMORY_FILE.read_text(encoding="utf-8")


def test_memory_home_and_doctor_warn_when_memory_file_is_unreadable() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('["broken",', encoding="utf-8")
    cfg = Config.load()

    assert "could not be loaded" in memory_runtime.command_text("home", cfg)
    doctor = json.loads(memory_runtime.command_text("doctor", cfg))
    assert doctor["ready"] is False
    assert doctor["memory_file_error"] == "unreadable"


def test_memory_load_error_is_not_persisted_to_config() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.memory_load_error = "unreadable"
    cfg.save()
    assert "memory_load_error" not in json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))


def test_repaired_memory_file_clears_load_warning_after_successful_write() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text("{bad", encoding="utf-8")
    os.chmod(config.MEMORY_FILE, 0o600)
    cfg = Config.load()
    assert cfg.memory_load_error == "unreadable"

    config.MEMORY_FILE.write_text("[]", encoding="utf-8")
    assert memory_runtime.remember_fact(cfg, "We use uv for all Python installs here") is True

    assert cfg.memory_load_error == ""
    assert "could not be loaded" not in memory_runtime.command_text("home", cfg)
    doctor = json.loads(memory_runtime.command_text("doctor", cfg))
    assert "memory_file_error" not in doctor


@pytest.mark.parametrize("payload", ["", "  \n"], ids=["empty", "whitespace"])
def test_blank_memory_file_is_treated_as_empty_list(payload: str) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text(payload, encoding="utf-8")
    os.chmod(config.MEMORY_FILE, 0o600)
    cfg = Config.load()
    assert cfg.memory_load_error == ""

    assert memory_runtime.remember_fact(cfg, "We use uv for all Python installs here") is True
    assert json.loads(config.MEMORY_FILE.read_text(encoding="utf-8")) == ["We use uv for all Python installs here"]


def test_missing_memory_file_still_accepts_first_fact() -> None:
    cfg = Config.load()
    assert cfg.memory_load_error == ""
    assert memory_runtime.remember_fact(cfg, "the build tool is uv") is True
    assert json.loads(config.MEMORY_FILE.read_text(encoding="utf-8")) == ["the build tool is uv"]


# --- invalid legacy facts must not block new writes ---


@pytest.mark.parametrize(
    "bad_fact",
    [pytest.param("x " * 2100, id="oversized"), pytest.param("colour \x1b[31mred", id="control-char")],
)
def test_invalid_legacy_fact_does_not_block_remember(bad_fact: str) -> None:
    _write_memory([bad_fact, "my shell is zsh"])
    cfg = Config.load()

    assert memory_runtime.remember_fact(cfg, "my editor is neovim") is True

    stored = json.loads(config.MEMORY_FILE.read_text(encoding="utf-8"))
    assert stored == [bad_fact, "my shell is zsh", "my editor is neovim"]
    contents = {record["content"] for record in memory_runtime.MemoryCatalog().records()}
    assert {"my shell is zsh", "my editor is neovim"} <= contents


def test_sync_legacy_facts_counts_skipped_and_doctor_reports_them() -> None:
    catalog = memory_runtime.MemoryCatalog()
    result = catalog.sync_legacy_facts(["x " * 2100, "fine fact here"], authoritative=False)

    assert result["skipped"] == 1
    assert result["added"] == 1
    status = catalog.doctor(["x " * 2100, "fine fact here"])
    assert status["invalid_legacy_facts"] == 1
    assert "Skipped 1 stored fact" in memory_runtime.home_text(catalog, ["x " * 2100, "fine fact here"])


# --- lessons chunking ---


def test_paragraph_lessons_without_headings_are_chunked() -> None:
    text = identity.DEFAULT_LESSONS + (
        "\nAlways quote Windows paths that contain spaces.\n\n"
        "Prefer uv over pip when installing Python tools.\n"
    )

    assert identity._chunk_lessons(text) == [
        "Always quote Windows paths that contain spaces.",
        "Prefer uv over pip when installing Python tools.",
    ]


def test_short_appended_lesson_is_kept() -> None:
    identity.scaffold_if_needed()
    identity.append_lesson("Use uv")

    chunks = identity._chunk_lessons(identity.LESSONS_PATH.read_text(encoding="utf-8"))

    assert len(chunks) == 1
    assert chunks[0].startswith("## ") and chunks[0].endswith("\nUse uv")


def test_template_only_lessons_file_has_no_chunks() -> None:
    assert identity._chunk_lessons(identity.DEFAULT_LESSONS) == []


def test_paragraph_lessons_reach_retrieval_index() -> None:
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        identity.DEFAULT_LESSONS + "\nThe footer toolbar needs noreverse.\n\nThe rust indexer is cold-start only.\n",
        encoding="utf-8",
    )

    result = identity.rebuild_lessons_index(lambda texts: [[1.0, float(i)] for i in range(len(texts))], "m")

    assert result["chunk_count"] == 2


# --- standing-rule auto-capture ---


@pytest.mark.parametrize(
    "text",
    [
        "You always forget to run the tests.",
        "You never read the file before editing it.",
        "I never said to delete that branch.",
        "I always used vim at my old job.",
    ],
)
def test_non_directive_always_never_sentences_are_not_eligible(text: str) -> None:
    candidates = memory_candidates.extract_candidates(text)
    assert [candidate.marker for candidate in candidates] == ["standing_rule"]
    decision = memory_candidates.evaluate_candidate(candidates[0])
    assert (decision.eligible, decision.reason) == (False, "not_directive")


@pytest.mark.parametrize(
    "text",
    [
        "We never use pip directly.",
        "Always run the linter before committing.",
        "You should always ask before deleting files.",
        "We always need two reviewers on releases.",
        "You should always tell me before pushing to main.",
        "You should always say which files you changed before committing.",
        "We should never tell customers the internal cost basis.",
        "You should always red-team new prompts before shipping them.",
        "You always read CLAUDE.md before editing files in this repo.",
    ],
)
def test_directive_standing_rules_stay_eligible(text: str) -> None:
    candidate = memory_candidates.extract_candidates(text)[0]
    assert memory_candidates.evaluate_candidate(candidate).reason == "eligible"


# --- compaction summarizer failure ---


class _FailingClient:
    def chat(self, **_kwargs):
        raise ConnectionError("maintenance model unreachable")


def _long_history() -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 900} for i in range(40)]


def test_summarizer_failure_returns_marked_fallback_summary() -> None:
    cfg = Config()
    summary = context_budget.summarize_message_batch(
        cfg,
        [{"role": "user", "content": "hello"}],
        maintenance_client_fn=lambda *_args: (_FailingClient(), "maintenance"),
    )

    assert isinstance(summary, context_budget.FallbackSummary)
    assert summary == "user: hello"


def test_fallback_compaction_warns_user(monkeypatch) -> None:
    cfg = Config()
    cfg.model_adaptive = False
    cfg.messages = _long_history()
    notices: list[str] = []
    monkeypatch.setattr(context_budget, "show_info", notices.append)
    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda *a, **k: 9000)
    monkeypatch.setattr(main, "small_maintenance_client", lambda *_args: (_FailingClient(), "maintenance"))

    compacted = context_budget.maybe_compact_context(
        client=None, cfg=cfg, model_info={"context_length": 131072, "parameter_size": "8B"}
    )

    assert compacted is True
    assert type(cfg.session_summary) is str
    assert len(notices) == 1 and "lossy fallback" in notices[0]


def test_fallback_manual_rebuild_reports_lossy_summary(monkeypatch) -> None:
    cfg = Config()
    cfg.messages = _long_history()
    monkeypatch.setattr(main, "small_maintenance_client", lambda *_args: (_FailingClient(), "maintenance"))

    ok, message = context_budget.rebuild_context_summary(None, cfg)  # type: ignore[arg-type]

    assert ok is True
    assert "lossy fallback" in message
