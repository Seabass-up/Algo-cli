from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os

import pytest

from algo_cli.henry_effect_control import (
    EffectLeaseBusy,
    EffectLeaseStateError,
    TargetLeaseManager,
    target_digest,
)


def _hold_target_lease(root, ready, release) -> None:
    with TargetLeaseManager(root, lock_timeout_seconds=2.0).acquire("shared-target") as lease:
        ready.put(lease.fencing_token)
        release.wait(timeout=5.0)


def test_fencing_tokens_increase_across_release_and_manager_restart(tmp_path) -> None:
    root = tmp_path / "leases"
    first_manager = TargetLeaseManager(root)
    first = first_manager.acquire("workspace:/repo/file")
    assert first.fencing_token == 1
    first.release()

    second_manager = TargetLeaseManager(root)
    second = second_manager.acquire("workspace:/repo/file")
    assert second.fencing_token == 2
    assert second.validate() is True
    second.release()


def test_same_target_is_single_writer_but_different_targets_can_progress(tmp_path) -> None:
    root = tmp_path / "leases"
    manager = TargetLeaseManager(root, lock_timeout_seconds=0.05)
    held = manager.acquire("target:one")

    with pytest.raises(EffectLeaseBusy):
        TargetLeaseManager(root, lock_timeout_seconds=0.02).acquire("target:one")
    other = manager.acquire("target:two")
    assert other.validate() is True
    other.release()
    held.release()


def test_fencing_token_consumption_is_serial_under_threads(tmp_path) -> None:
    root = tmp_path / "leases"

    def worker(_index: int) -> int:
        with TargetLeaseManager(root).acquire("same-target") as lease:
            return lease.fencing_token

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(worker, range(8)))

    assert sorted(tokens) == list(range(1, 9))


def test_corrupt_state_fails_closed_without_resetting_fence(tmp_path) -> None:
    root = tmp_path / "leases"
    manager = TargetLeaseManager(root)
    root.mkdir()
    digest = target_digest("target")
    (root / f"{digest}.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(EffectLeaseStateError):
        manager.acquire("target")


def test_owner_identifiers_are_bounded_and_non_sensitive(tmp_path) -> None:
    manager = TargetLeaseManager(tmp_path / "leases")

    with pytest.raises(ValueError, match="owner_id"):
        manager.acquire("target", owner_id="private user content")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_symlink_state_is_rejected(tmp_path) -> None:
    root = tmp_path / "leases"
    root.mkdir()
    digest = target_digest("target")
    destination = tmp_path / "outside.json"
    destination.write_text("{}", encoding="utf-8")
    (root / f"{digest}.json").symlink_to(destination)

    with pytest.raises(EffectLeaseStateError):
        TargetLeaseManager(root).acquire("target")


def test_released_or_foreign_lease_cannot_validate(tmp_path) -> None:
    root = tmp_path / "leases"
    first_manager = TargetLeaseManager(root)
    lease = first_manager.acquire("target")
    assert TargetLeaseManager(root).validate(lease) is False
    lease.release()
    assert lease.validate() is False


def test_expired_or_backward_clock_lease_cannot_validate(tmp_path) -> None:
    now = [10.0]
    manager = TargetLeaseManager(tmp_path / "leases", clock=lambda: now[0])
    lease = manager.acquire("target", lease_seconds=5.0)
    assert lease.validate() is True

    now[0] = 9.0
    assert lease.validate() is False
    now[0] = 15.0
    assert lease.validate() is False
    lease.release()


def test_same_target_is_single_writer_across_processes(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    process = context.Process(
        target=_hold_target_lease,
        args=(str(tmp_path / "leases"), ready, release),
    )
    process.start()
    try:
        assert ready.get(timeout=5.0) == 1
        with pytest.raises(EffectLeaseBusy):
            TargetLeaseManager(
                tmp_path / "leases",
                lock_timeout_seconds=0.05,
            ).acquire("shared-target")
    finally:
        release.set()
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
    assert process.exitcode == 0

    next_lease = TargetLeaseManager(tmp_path / "leases").acquire("shared-target")
    assert next_lease.fencing_token == 2
    next_lease.release()
