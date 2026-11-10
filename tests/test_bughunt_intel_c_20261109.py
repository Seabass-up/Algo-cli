"""Regression tests for bug-hunt intel batch C (2026-11-09).

D1 process_framework: a false step condition follows on_failure, not on_success.
D2 log2_histogram: percentiles in the overflow bucket are finite; labels match boundaries.
D3 ralph_loop: pytest summary parsing counts failed/error by name; failing runs never PASS.
D4 minhash_lsh: re-insert and remove leave no stale bucket entries; query_similar skips ghosts.
"""

from __future__ import annotations

import json

from algo_cli.intelligence.log2_histogram import BOUNDARIES_US, BUCKET_LABELS, Log2Histogram
from algo_cli.intelligence.minhash_lsh import LSHIndex, MinHasher
from algo_cli.intelligence.process_framework import (
    Process,
    ProcessState,
    ProcessStep,
    make_bid_process,
    make_permit_process,
)
from algo_cli.intelligence.ralph_loop import LoopStatus, RalphLoop


# ── D1: process_framework gate failure routing ──────────────────────────────


def test_d1_bid_without_win_ends_lost_not_won():
    p = make_bid_process()
    ctx = p.run({"estimate_total": 1000})
    assert ctx.get("status") == "lost"
    assert p.steps["check_result"].state == ProcessState.SKIPPED
    assert p.steps["won"].state == ProcessState.PENDING


def test_d1_bid_with_win_still_ends_won():
    p = make_bid_process()
    ctx = p.run({"estimate_total": 1000, "won": True})
    assert ctx.get("status") == "won"
    assert p.steps["lost"].state == ProcessState.PENDING


def test_d1_rejected_permit_takes_corrections_not_approval():
    p = make_permit_process()
    ctx = p.run({"approved": False})
    assert ctx.get("corrections_made") is True
    assert "permit_approved" not in ctx
    assert p.steps["approval"].state == ProcessState.PENDING
    assert p.steps["review"].state == ProcessState.SKIPPED


def test_d1_approved_permit_unchanged():
    p = make_permit_process()
    ctx = p.run({"approved": True})
    assert ctx.get("permit_approved") is True
    assert ctx.get("inspection_scheduled") is True
    assert "corrections_made" not in ctx


def test_d1_resume_from_gate_failure_follows_on_failure():
    p = make_permit_process()
    ctx = p.resume_from("review")
    assert ctx.get("corrections_made") is True
    assert "permit_approved" not in ctx


def test_d1_gate_false_without_on_failure_stops_process():
    ran: list[str] = []

    def mark(name: str):
        def action(ctx: dict) -> dict:
            ran.append(name)
            return {}
        return action

    p = Process("gate_stop")
    p.add_step(ProcessStep(name="gate", action=mark("gate"), condition=lambda ctx: False,
                           on_success="after"), is_start=True)
    p.add_step(ProcessStep(name="after", action=mark("after")))
    p.run({})
    assert ran == []
    assert p.steps["gate"].state == ProcessState.SKIPPED
    assert p.completed is True


# ── D2: log2_histogram overflow percentile and labels ───────────────────────


def test_d2_overflow_percentile_is_finite():
    h = Log2Histogram()
    h.observe_seconds(40)
    value = h.percentile(50)
    assert value == 40_000_000
    assert h.percentile(99) == 40_000_000


def test_d2_summary_and_json_have_no_infinity():
    h = Log2Histogram()
    h.observe_seconds(40)
    summary = h.summary()
    assert all(v == v and v not in (float("inf"), float("-inf")) for v in summary.values())
    assert "Infinity" not in h.to_json()
    json.loads(h.to_json())


def test_d2_overflow_percentile_without_max_falls_back_to_lower_bound():
    h = Log2Histogram(buckets=[0] * len(BOUNDARIES_US), count=1, sum_us=0.0, max_us=0.0)
    h.buckets[-1] = 1
    assert h.percentile(50) == BOUNDARIES_US[-2]


def test_d2_labels_state_real_boundaries():
    expected = {
        12: "<=10.49s",
        13: "<=20.97s",
        14: "<=31.46s",
    }
    for idx, label in expected.items():
        assert BUCKET_LABELS[idx] == label
    assert len(BUCKET_LABELS) == len(BOUNDARIES_US)


# ── D3: ralph_loop pytest summary parsing ───────────────────────────────────


def test_d3_failed_only_summary_counts_failed():
    run = RalphLoop.parse_pytest_output("=== 3 failed in 0.12s ===")
    assert run.failed == 3
    assert run.passed == 0


def test_d3_failed_only_summary_is_not_passing():
    result = RalphLoop(max_iterations=1).run(
        lambda: RalphLoop.parse_pytest_output("=== 3 failed in 0.12s ===")
    )
    assert result.status != LoopStatus.PASSING


def test_d3_error_summary_is_not_passing():
    out = "2 passed, 1 error in 0.3s"
    run = RalphLoop.parse_pytest_output(out)
    assert run.passed == 2
    assert run.errors == 1
    result = RalphLoop(max_iterations=1).run(lambda: RalphLoop.parse_pytest_output(out))
    assert result.status != LoopStatus.PASSING


def test_d3_plural_errors_counted():
    run = RalphLoop.parse_pytest_output("=== 2 errors in 0.1s ===")
    assert run.errors == 2


def test_d3_xfailed_is_not_a_failure():
    out = "=== 3 passed, 1 xfailed in 0.20s ==="
    run = RalphLoop.parse_pytest_output(out)
    assert run.passed == 3
    assert run.failed == 0
    assert run.errors == 0
    result = RalphLoop(max_iterations=1).run(lambda: RalphLoop.parse_pytest_output(out))
    assert result.status == LoopStatus.PASSING


def test_d3_mixed_failed_and_passed_counts():
    run = RalphLoop.parse_pytest_output("===== 1 failed, 2 passed, 1 warning in 1.00s =====")
    assert run.failed == 1
    assert run.passed == 2


def test_d3_nonzero_exit_without_summary_is_not_passing():
    result = RalphLoop(max_iterations=1).run(
        lambda: RalphLoop.parse_pytest_output("ImportError: no module named x", exit_code=2)
    )
    assert result.status != LoopStatus.PASSING


def test_d3_clean_pass_still_passing():
    result = RalphLoop(max_iterations=1).run(
        lambda: RalphLoop.parse_pytest_output("=== 5 passed in 0.10s ===", exit_code=0)
    )
    assert result.status == LoopStatus.PASSING


# ── D4: minhash_lsh stale bucket entries ────────────────────────────────────


def _bucket_members(index: LSHIndex, key: str) -> list:
    return [bk for bk, members in index._buckets.items() if key in members]


def test_d4_reinsert_removes_old_bucket_entries():
    idx = LSHIndex(num_bands=4, rows_per_band=2)
    old_sig = list(range(8))
    new_sig = list(range(100, 108))
    idx.insert("a", old_sig)
    idx.insert("a", new_sig)
    assert idx.query(old_sig) == []
    assert idx.query(new_sig) == ["a"]
    assert len(_bucket_members(idx, "a")) == idx.num_bands


def test_d4_remove_after_reinsert_leaves_no_entries():
    idx = LSHIndex(num_bands=4, rows_per_band=2)
    old_sig = list(range(8))
    new_sig = list(range(100, 108))
    idx.insert("a", old_sig)
    idx.insert("a", new_sig)
    idx.remove("a")
    assert _bucket_members(idx, "a") == []
    assert idx.query(old_sig) == []


def test_d4_query_similar_after_remove_does_not_raise():
    idx = LSHIndex(num_bands=4, rows_per_band=2)
    hasher = MinHasher(num_hashes=8)
    old_sig = list(range(8))
    idx.insert("a", old_sig)
    idx.insert("a", list(range(100, 108)))
    idx.remove("a")
    assert idx.query_similar(old_sig, hasher, threshold=0.0) == []


def test_d4_query_similar_skips_candidate_without_signature():
    idx = LSHIndex(num_bands=4, rows_per_band=2)
    hasher = MinHasher(num_hashes=8)
    sig = list(range(8))
    idx.insert("real", sig)
    ghost_bucket = next(iter(idx._buckets))
    idx._buckets[ghost_bucket].add("ghost")
    results = idx.query_similar(sig, hasher, threshold=0.0)
    assert [key for key, _ in results] == ["real"]


def test_d4_normal_query_similar_unchanged():
    idx = LSHIndex(num_bands=4, rows_per_band=2)
    hasher = MinHasher(num_hashes=8)
    sig = list(range(8))
    idx.insert("a", sig)
    results = idx.query_similar(sig, hasher, threshold=0.5)
    assert results == [("a", 1.0)]
