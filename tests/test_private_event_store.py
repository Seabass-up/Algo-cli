from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from algo_cli.private_event_store import (
    EventTooLargeError,
    PrivateEventStore,
    RetentionPolicy,
    UnsafeStorePathError,
)


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_store(
    tmp_path: Path,
    *,
    clock: Clock | None = None,
    max_records: int = 20,
    max_bytes: int = 16_000,
    max_age_seconds: float = 3_600,
) -> PrivateEventStore:
    return PrivateEventStore(
        tmp_path / "private" / "events.jsonl",
        policy=RetentionPolicy(
            max_records=max_records,
            max_bytes=max_bytes,
            max_age_seconds=max_age_seconds,
        ),
        clock=clock or Clock(),
    )


def test_append_and_read_round_trip_uses_store_owned_envelope(tmp_path: Path) -> None:
    clock = Clock(1234.5)
    store = make_store(tmp_path, clock=clock)

    result = store.append({"kind": "lesson", "value": "private"})

    assert result.stored is True
    assert result.retention_satisfied is True
    assert store.read_events() == [{"kind": "lesson", "value": "private"}]
    records = store.read_records()
    assert len(records) == 1
    assert records[0].stored_at == 1234.5
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw == {
        "event": {"kind": "lesson", "value": "private"},
        "stored_at": 1234.5,
        "version": 1,
    }


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX modes are not portable to Windows")
def test_initialize_and_append_repair_private_permissions(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.initialize()
    os.chmod(store.path.parent, 0o777)
    os.chmod(store.path, 0o666)
    os.chmod(store.lock_path, 0o666)

    store.append({"sequence": 1})

    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_concurrent_appends_are_complete_and_not_lost(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_records=100, max_bytes=100_000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: store.append({"sequence": value}), range(80)))

    events = store.read_events()
    assert all(result.stored for result in results)
    assert len(events) == 80
    assert {event["sequence"] for event in events} == set(range(80))
    assert all(json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines())
    # The lock sidecar contains one byte rather than growing per acquisition.
    assert store.lock_path.stat().st_size == 1


def test_record_retention_keeps_newest_suffix(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_records=3)

    results = [store.append({"sequence": value}) for value in range(6)]

    assert [event["sequence"] for event in store.read_events()] == [3, 4, 5]
    assert results[-1].compacted is True
    assert results[-1].dropped_records == 1
    assert results[-1].retention_satisfied is True
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 3


def test_byte_retention_keeps_newest_complete_records(tmp_path: Path) -> None:
    # First determine one encoded line's exact size, then permit only two.
    probe = make_store(tmp_path / "probe")
    probe.append({"value": "x" * 20})
    line_bytes = probe.path.stat().st_size
    store = make_store(
        tmp_path / "bounded",
        max_records=20,
        max_bytes=(line_bytes * 2) + 2,
    )

    for value in range(5):
        store.append({"value": "x" * 20, "sequence": value})

    events = store.read_events()
    assert 1 <= len(events) <= 2
    assert events[-1]["sequence"] == 4
    assert store.path.stat().st_size <= store.policy.max_bytes


def test_age_retention_uses_store_timestamp_not_event_fields(tmp_path: Path) -> None:
    clock = Clock(100.0)
    store = make_store(tmp_path, clock=clock, max_age_seconds=10)
    store.append({"claimed_timestamp": 10_000, "sequence": 1})
    clock.value = 105.0
    store.append({"claimed_timestamp": 0, "sequence": 2})
    clock.value = 111.0

    result = store.compact()

    assert result.compacted is True
    assert result.retention_satisfied is True
    assert store.read_events() == [{"claimed_timestamp": 0, "sequence": 2}]


def test_malformed_tail_is_ignored_then_removed_without_losing_next_append(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append({"sequence": 1})
    with store.path.open("ab") as handle:
        handle.write(b'{"event":"torn"')

    assert store.read_events() == [{"sequence": 1}]
    degraded = store.readiness()
    assert degraded["status"] == "degraded"
    assert degraded["malformed_records"] == 1

    result = store.append({"sequence": 2})

    assert result.stored is True
    assert result.compacted is True
    assert result.malformed_records == 1
    assert store.read_events() == [{"sequence": 1}, {"sequence": 2}]
    assert store.readiness()["status"] == "ready"


def test_atomic_compaction_failure_preserves_original_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.append({"sequence": 1})
    with store.path.open("ab") as handle:
        handle.write(b"not-json\n")
    before = store.path.read_bytes()

    def fail_replace(_source: Any, _target: Any) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr("algo_cli.private_event_store.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.compact()

    assert store.path.read_bytes() == before
    assert store.read_events() == [{"sequence": 1}]


def test_append_remains_durable_when_followup_compaction_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path, max_records=1)
    store.append({"sequence": 1})

    def fail_replace(_source: Any, _target: Any) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr("algo_cli.private_event_store.os.replace", fail_replace)
    result = store.append({"sequence": 2})

    assert result.stored is True
    assert result.compacted is False
    assert result.retention_satisfied is False
    assert result.error_type == "OSError"
    raw_events = [json.loads(line)["event"] for line in store.path.read_text().splitlines()]
    assert raw_events == [{"sequence": 1}, {"sequence": 2}]
    # Reads still apply the in-memory retention suffix even before repair.
    assert store.read_events() == [{"sequence": 2}]


def test_oversized_or_unserializable_event_never_touches_existing_file(tmp_path: Path) -> None:
    store = make_store(tmp_path, max_bytes=180)
    store.append({"sequence": 1})
    before = store.path.read_bytes()

    with pytest.raises(EventTooLargeError):
        store.append({"value": "x" * 1_000})
    with pytest.raises(ValueError, match="finite JSON object"):
        store.append({"value": object()})

    assert store.path.read_bytes() == before


def test_readiness_is_aggregate_and_never_exposes_content_or_path(tmp_path: Path) -> None:
    secret = "top-secret-value"
    store = make_store(tmp_path)
    store.append({"secret": secret, "kind": "credential"})

    report = store.readiness()
    rendered = json.dumps(report, sort_keys=True)

    assert report["status"] == "ready"
    assert report["records"] == 1
    assert report["retained_records"] == 1
    assert report["compaction_needed"] is False
    assert secret not in rendered
    assert "credential" not in rendered
    assert str(tmp_path) not in rendered
    assert "event" not in report


def test_readiness_repairs_modes_and_empty_store_is_not_an_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    report = store.readiness()

    assert report["status"] == "empty"
    assert report["initialized"] is False
    assert report["records"] == 0
    assert report["error_type"] is None
    if os.name == "posix":
        assert report["directory_private"] is True
        assert report["lock_private"] is True


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX modes are not portable to Windows")
def test_readiness_can_observe_broad_modes_without_repairing_them(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.initialize()
    os.chmod(store.path.parent, 0o755)
    os.chmod(store.path, 0o644)
    os.chmod(store.lock_path, 0o644)

    report = store.readiness(repair_permissions=False)

    assert report["status"] == "degraded"
    assert report["directory_private"] is False
    assert report["file_private"] is False
    assert report["lock_private"] is False
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o644


def test_rejects_symlink_store_instead_of_following_it(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    target = tmp_path / "target.jsonl"
    target.write_text("sentinel", encoding="utf-8")
    path = tmp_path / "private" / "events.jsonl"
    path.parent.mkdir()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable in this environment")
    store = PrivateEventStore(path)

    with pytest.raises(UnsafeStorePathError):
        store.append({"sequence": 1})

    assert target.read_text(encoding="utf-8") == "sentinel"
    report = store.readiness()
    assert report["status"] == "error"
    assert report["error_type"] == "UnsafeStorePathError"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_records": 0},
        {"max_records": 1.5},
        {"max_bytes": 0},
        {"max_bytes": 1.5},
        {"max_age_seconds": 0},
        {"max_age_seconds": float("inf")},
    ],
)
def test_retention_policy_requires_all_three_finite_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(**kwargs)
