"""Concurrency and persistence coverage for performance telemetry."""

from __future__ import annotations

import json
import threading
from typing import Any

from algo_cli import perf_telemetry


def test_flush_atomically_swaps_buffer_while_producers_continue(monkeypatch: Any) -> None:
    entered = threading.Event()
    release = threading.Event()
    written: list[dict[str, Any]] = []

    class BlockingStore:
        @staticmethod
        def append(event: dict[str, Any]) -> None:
            entered.set()
            assert release.wait(timeout=2)
            written.append(event)

    first = {"event": "tool", "tool": "first"}
    second = {"event": "tool", "tool": "second"}
    monkeypatch.setattr(perf_telemetry, "PERF_BUFFER", [first])
    monkeypatch.setattr(perf_telemetry, "_private_perf_store", lambda: BlockingStore())

    errors: list[BaseException] = []

    def flush() -> None:
        try:
            perf_telemetry.flush_perf_records()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=flush)
    worker.start()
    assert entered.wait(timeout=2)

    perf_telemetry.append_perf_record(second)
    assert perf_telemetry.PERF_BUFFER == [second]

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not errors
    assert perf_telemetry.PERF_BUFFER == [second]
    assert written == [{"kind": "perf_batch", "records": [first]}]


def test_load_perf_history_reads_the_requested_tail(tmp_path: Any, monkeypatch: Any) -> None:
    history_path = tmp_path / "perf.jsonl"
    rows = [{"event": "tool", "sequence": sequence} for sequence in range(20)]
    history_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(perf_telemetry, "PERF_HISTORY_FILE", history_path)
    monkeypatch.setattr(perf_telemetry, "_PERF_TAIL_BLOCK_BYTES", 32)

    loaded = perf_telemetry.load_perf_history(limit=3)

    assert [row["sequence"] for row in loaded] == [17, 18, 19]


def test_flush_failure_is_nonfatal_and_keeps_only_bounded_suffix(monkeypatch: Any) -> None:
    class BrokenStore:
        @staticmethod
        def append(_event: dict[str, Any]) -> None:
            raise PermissionError("read-only config")

    monkeypatch.setattr(perf_telemetry, "_private_perf_store", lambda: BrokenStore())
    monkeypatch.setattr(perf_telemetry, "_PERF_BUFFER_MAX_RECORDS", 3)
    monkeypatch.setattr(
        perf_telemetry,
        "PERF_BUFFER",
        [{"sequence": index} for index in range(6)],
    )

    assert perf_telemetry.flush_perf_records() is False
    assert [item["sequence"] for item in perf_telemetry.PERF_BUFFER] == [3, 4, 5]


def test_model_round_receipts_buffer_instead_of_flushing_per_round(monkeypatch: Any) -> None:
    flushed: list[bool] = []
    monkeypatch.setattr(perf_telemetry, "PERF_BUFFER", [])
    monkeypatch.setattr(
        perf_telemetry,
        "flush_perf_records",
        lambda: flushed.append(True) or True,
    )

    perf_telemetry.append_perf_record({"event": "model_round", "round": 1})

    assert perf_telemetry.PERF_BUFFER == [{"event": "model_round", "round": 1}]
    assert flushed == []
