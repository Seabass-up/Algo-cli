from __future__ import annotations

import json

import pytest

from algo_cli import (
    agent_threads,
    code_rag,
    elsie_echo_preflight,
    harness,
    identity,
    julia_memory_candidates as memory_candidates,
    skills,
    ada_task_ledger as task_ledger,
    tools,
)
from algo_cli.config import Config
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


def _receipt_store() -> StaticKeyStore:
    return StaticKeyStore({PRIVACY_KEY_LABEL: b"e" * 32})


def test_protected_preflight_quarantines_before_index_invalidation(monkeypatch) -> None:
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    events: list[str] = []

    def prepare(**_kwargs) -> dict[str, int]:
        events.append("quarantine")
        return {"quarantined_unproven_active_skills": 2}

    def invalidate() -> int:
        events.append("invalidate")
        return 3

    monkeypatch.setattr(skills, "prepare_protected_skill_history", prepare)
    monkeypatch.setattr(
        harness,
        "configure_protected_memory_authority",
        lambda enabled: events.append(f"memory:{enabled}") or 4,
    )
    monkeypatch.setattr(
        tools,
        "purge_x_search_cache",
        lambda: events.append("x-search") or 5,
    )
    monkeypatch.setattr(
        code_rag,
        "purge_persisted_indexes",
        lambda: events.append("code-index") or 7,
    )
    monkeypatch.setattr(
        identity,
        "clear_plaintext_identity_cache",
        lambda: events.append("identity-cache") or 6,
    )
    monkeypatch.setattr(
        identity,
        "purge_legacy_lessons_index",
        lambda: events.append("lessons-index") or 1,
    )
    monkeypatch.setattr(
        task_ledger,
        "prepare_protected_goal_store",
        lambda **_kwargs: events.append("goal") or True,
    )
    monkeypatch.setattr(
        memory_candidates,
        "prepare_protected_candidate_state",
        lambda *_args, **_kwargs: events.append("candidate") or True,
    )
    monkeypatch.setattr(
        agent_threads,
        "prepare_protected_thread_store",
        lambda **_kwargs: events.append("thread") or True,
    )
    monkeypatch.setattr(harness, "invalidate_user_skill_records", invalidate)

    result = elsie_echo_preflight.prepare_echo_auxiliary_state(cfg)

    assert events == [
        "memory:True",
        "x-search",
        "code-index",
        "identity-cache",
        "lessons-index",
        "quarantine",
        "goal",
        "candidate",
        "thread",
        "invalidate",
    ]
    assert result == {
        "protected": True,
        "invalidated_skill_records": 3,
        "invalidated_mutable_memory_records": 4,
        "purged_x_search_cache_entries": 5,
        "purged_plaintext_code_index_entries": 7,
        "cleared_plaintext_identity_cache_entries": 6,
        "purged_legacy_lessons_index": True,
        "recovered_goal_store": True,
        "recovered_candidate_store": True,
        "recovered_thread_store": True,
        "quarantined_unproven_active_skills": 2,
    }


def test_unprotected_preflight_does_not_touch_auxiliary_stores(monkeypatch) -> None:
    cfg = Config(echo_veil_enabled=False)
    monkeypatch.setattr(
        skills,
        "prepare_protected_skill_history",
        lambda: (_ for _ in ()).throw(AssertionError("must not prepare")),
    )
    monkeypatch.setattr(
        harness,
        "invalidate_user_skill_records",
        lambda: (_ for _ in ()).throw(AssertionError("must not invalidate")),
    )
    states: list[bool] = []
    monkeypatch.setattr(
        harness,
        "configure_protected_memory_authority",
        lambda enabled: states.append(enabled) or 0,
    )

    assert elsie_echo_preflight.prepare_echo_auxiliary_state(cfg) == {
        "protected": False,
        "invalidated_skill_records": 0,
    }
    assert states == [False]


def test_protected_preflight_normalizes_failures_without_payload(monkeypatch) -> None:
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    canary = "PROTECTED_SKILL_CANARY"
    monkeypatch.setattr(
        skills,
        "prepare_protected_skill_history",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )

    try:
        elsie_echo_preflight.prepare_echo_auxiliary_state(cfg)
    except elsie_echo_preflight.EchoAuxiliaryPreflightError as exc:
        assert canary not in str(exc)
    else:
        raise AssertionError("protected preflight must fail closed")


def test_protected_preflight_retains_only_bounded_infrastructure_reason(monkeypatch) -> None:
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")

    def fail_with_bounded_cause(**_kwargs):
        try:
            raise RuntimeError("credential_registry_unavailable")
        except RuntimeError as exc:
            raise RuntimeError("private outer detail") from exc

    monkeypatch.setattr(skills, "prepare_protected_skill_history", fail_with_bounded_cause)

    with pytest.raises(elsie_echo_preflight.EchoAuxiliaryPreflightError) as captured:
        elsie_echo_preflight.prepare_echo_auxiliary_state(cfg)

    assert captured.value.reason_code == "credential_registry_unavailable"
    assert "private outer detail" not in str(captured.value)

    shaped_canary = "credential_secret_payload_canary_123"
    rejected = elsie_echo_preflight.EchoAuxiliaryPreflightError.from_exception(RuntimeError(shaped_canary))
    assert rejected.reason_code == "echo_auxiliary_unavailable"
    assert shaped_canary not in str(rejected)


def test_protected_preflight_removes_cached_plaintext_skill_canary(
    config_dir,
) -> None:
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    skills.ensure_dirs()
    user_skill = skills.SKILLS_DIR / "legacy-canary.md"
    canary = "PROTECTED_CACHED_SKILL_RETRIEVAL_CANARY"
    user_skill.write_text(f"# Legacy\n\n{canary}\n", encoding="utf-8")
    payload = {
        "generated": "2026-08-10T00:00:00",
        "source_policy": harness._source_policy(),
        "record_count": 1,
        "roots": [],
        "indexer": "python",
        "records": [
            {
                "id": "algo-cli:skill:legacy-canary.md",
                "harness": "algo-cli",
                "kind": "skill",
                "title": "legacy canary",
                "path": str(user_skill),
                "relative_path": "legacy-canary.md",
                "search_text": canary,
            }
        ],
        "embeddings": {"active_model": harness.DEFAULT_EMBED_MODEL},
    }
    harness.INDEX_PATH.write_text(json.dumps(payload), encoding="utf-8")
    harness._set_index_cache(payload, persisted=True, sources_current=True)
    assert harness.search_index(canary, limit=5)

    store = _receipt_store()
    result = elsie_echo_preflight.prepare_echo_auxiliary_state(
        cfg,
        receipt_key_store=store,
        receipt_anchor_store=store,
    )

    assert result["quarantined_unproven_active_skills"] == 1
    assert result["invalidated_skill_records"] == 0
    assert result["invalidated_mutable_memory_records"] == 1
    assert harness.search_index(canary, limit=5) == []
    assert canary not in harness.INDEX_PATH.read_text(encoding="utf-8")
    assert not user_skill.exists()


def test_protected_preflight_purges_and_prevents_mutable_memory_index_records(
    tmp_path,
    config_dir,
) -> None:
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    memory_root = tmp_path / "external-memory"
    memory_root.mkdir()
    prompt_root = tmp_path / "openclaw-workspace"
    prompt_root.mkdir()
    extra_root = tmp_path / "relabeled-extra"
    extra_root.mkdir()
    canary = "EXTERNAL_MUTABLE_MEMORY_INDEX_CANARY"
    (memory_root / "memory.md").write_text(canary, encoding="utf-8")
    (prompt_root / "USER.md").write_text(canary, encoding="utf-8")
    (prompt_root / "lessons-learned.md").write_text(canary, encoding="utf-8")
    (extra_root / "evidence.md").write_text(canary, encoding="utf-8")
    x_cache = config_dir / "x_search_cache"
    x_cache.mkdir()
    (x_cache / "cached.md").write_text(canary, encoding="utf-8")
    code_rag.CODE_INDEX_DIR.mkdir(parents=True)
    (code_rag.CODE_INDEX_DIR / "legacy-index.json").write_text(
        json.dumps({"chunks": [{"text": canary}]}),
        encoding="utf-8",
    )
    identity.IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    identity.LESSONS_INDEX_PATH.write_text(
        json.dumps({"chunks": [{"text": canary}]}),
        encoding="utf-8",
    )
    identity._CACHE[identity.USER_PATH] = identity.CacheEntry(
        mtime_ns=1,
        content=canary,
    )
    payload = {
        "generated": "2026-08-10T00:00:00",
        "source_policy": harness._source_policy(),
        "record_count": 3,
        "roots": [],
        "indexer": "python",
        "records": [
            {
                "id": "codex:memory:memory.md",
                "harness": "codex",
                "kind": "memory",
                "path": str(memory_root / "memory.md"),
                "relative_path": "memory.md",
                "search_text": canary,
            },
            canary,
            {
                "id": "algo-cli:skill:safe.md",
                "harness": "algo-cli",
                "kind": "skill",
                "path": str(tmp_path / "safe.md"),
                "relative_path": "safe.md",
                "search_text": "safe record",
            },
        ],
        "embeddings": {"active_model": harness.DEFAULT_EMBED_MODEL},
    }
    harness.INDEX_PATH.write_text(json.dumps(payload), encoding="utf-8")
    harness.SOURCE_ROOTS = (
        harness.SourceRoot("codex", "memory", memory_root, ("*.md",), 10),
        harness.SourceRoot(
            "openclaw",
            "prompt",
            prompt_root,
            ("USER.md", "lessons-learned.md"),
            10,
        ),
        harness.SourceRoot("custom", "wiki", extra_root, ("*.md",), 10),
    )
    store = _receipt_store()

    result = elsie_echo_preflight.prepare_echo_auxiliary_state(
        cfg,
        receipt_key_store=store,
        receipt_anchor_store=store,
    )

    assert result["invalidated_mutable_memory_records"] == 3
    assert result["purged_x_search_cache_entries"] == 1
    assert result["purged_legacy_lessons_index"] is True
    assert not x_cache.exists()
    assert not code_rag.CODE_INDEX_DIR.exists()
    assert not identity.LESSONS_INDEX_PATH.exists()
    assert identity._CACHE == {}
    persisted = harness.INDEX_PATH.read_text(encoding="utf-8")
    assert canary not in persisted
    assert harness.all_source_roots() == ()
    rebuilt = harness.build_index()
    assert canary not in json.dumps(rebuilt)


def test_echo_preflight_on_then_off_restores_memory_source_policy(tmp_path) -> None:
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    root = harness.SourceRoot("codex", "memory", memory_root, ("*.md",), 10)
    harness.SOURCE_ROOTS = (root,)
    store = _receipt_store()

    elsie_echo_preflight.prepare_echo_auxiliary_state(
        Config(echo_veil_enabled=True, echo_veil_protection="required"),
        receipt_key_store=store,
        receipt_anchor_store=store,
    )
    assert root not in harness.all_source_roots()

    elsie_echo_preflight.prepare_echo_auxiliary_state(Config(echo_veil_enabled=False))

    assert root in harness.all_source_roots()
