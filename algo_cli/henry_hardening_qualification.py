"""Disabled adversarial qualification helpers for the active hardening freeze.

This module is evidence tooling, not a runtime feature.  It exercises the
finite control boundary with deterministic local fixtures and reports missing
live evidence as blocked instead of converting simulations into production
claims.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Mapping

from . import action_registry, tools
from .ada_control_journal import ControlEffectState, ControlJournal
from .david_control_kernel import (
    ControlDataClass,
    ControlEnvelope,
    ControlPolicy,
    ControlRequest,
    ControlRoute,
    ControlSigner,
    Operation,
    SnapshotRef,
    TargetKind,
    TargetRef,
    default_control_policy,
    issue_grant,
    issue_permit,
)
from .david_control_runtime import (
    AdapterDispatchResult,
    AdapterReconciliationResult,
    ControlRuntime,
    DispatchDisposition,
    ReconciliationDisposition,
    fresh_postcondition_evidence,
    structural_evidence,
)
from .evals.tool_context_efficiency import assert_tool_context_efficiency
from .irene_privacy_views import PrivacyView, project_action_args
from .marcus_authority import (
    Capability,
    ConfirmationMode,
    CURATED_TOOL_POLICIES,
    EffectClass,
    policy_for_action,
)
from .nathan_program_runtime import (
    ProgramValidationError,
    authorization_for_actions,
    compile_program,
)


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_SEED = 0x48_45_4E_52_59
QUALIFICATION_NOW_MS = 1_800_000_000_000
MIN_RACE_TRIALS = 10_000
MIN_PROTOCOL_FRAMES = 100_000
MIN_POSTCONDITION_TRIALS = 1_000
MIN_UNKNOWN_OUTCOME_TRIALS = 1_000
MIN_PROGRAM_REJECTION_TRIALS = 1_000
MIN_PRIVACY_CANARIES = 1_000
MIN_EFFICIENCY_REPETITIONS = 5

_DIGEST_PREFIX = "sha256:"
_ALLOWED_STATUS = frozenset({"pass", "fail", "blocked", "not_verified"})


def _uuid(number: int) -> str:
    if type(number) is not int or not 1 <= number <= 999_999_999_999:
        raise ValueError("qualification_uuid")
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return round(ordered[position], 3)


def wilson_interval(successes: int, trials: int, *, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a bounded 95 percent Wilson interval for a binomial proportion."""

    if type(successes) is not int or type(trials) is not int or not 0 <= successes <= trials or trials <= 0:
        raise ValueError("qualification_interval")
    proportion = successes / trials
    denominator = 1.0 + (z * z / trials)
    center = (proportion + (z * z / (2.0 * trials))) / denominator
    margin = (
        z
        * ((proportion * (1.0 - proportion) / trials) + (z * z / (4.0 * trials * trials))) ** 0.5
        / denominator
    )
    return (round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6))


@dataclass(frozen=True, slots=True)
class HenryQualificationMetric:
    metric_id: str
    status: str
    numerator: int | None
    denominator: int | None
    threshold: str
    scope: str
    limitations: str
    measurements: Mapping[str, int | float | str | bool]

    def __post_init__(self) -> None:
        if not self.metric_id or self.status not in _ALLOWED_STATUS:
            raise ValueError("qualification_metric")
        if self.numerator is None or self.denominator is None:
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("qualification_denominator")
        elif (
            type(self.numerator) is not int
            or type(self.denominator) is not int
            or not 0 <= self.numerator <= self.denominator
            or self.denominator <= 0
        ):
            raise ValueError("qualification_denominator")
        if not self.threshold or not self.scope or not self.limitations:
            raise ValueError("qualification_description")
        for key, value in self.measurements.items():
            if type(key) is not str or not key or type(value) not in {int, float, str, bool}:
                raise ValueError("qualification_measurement")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.metric_id,
            "status": self.status,
            "threshold": self.threshold,
            "scope": self.scope,
            "limitations": self.limitations,
            "measurements": dict(sorted(self.measurements.items())),
        }
        if self.numerator is not None and self.denominator is not None:
            low, high = wilson_interval(self.numerator, self.denominator)
            value.update(
                {
                    "numerator": self.numerator,
                    "denominator": self.denominator,
                    "rate": round(self.numerator / self.denominator, 6),
                    "wilson_95": [low, high],
                }
            )
        return value


@dataclass(frozen=True, slots=True)
class _QualificationFixture:
    signer: ControlSigner
    policy: ControlPolicy
    target: TargetRef
    snapshot: SnapshotRef


def _fixture() -> _QualificationFixture:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    policy = default_control_policy()
    target = TargetRef.from_dict(
        {
            "kind": TargetKind.BROWSER_DOCUMENT.value,
            "target_id": _opaque("henry-qualification-target"),
            "epoch": 7,
            "revision": "document-7",
            "fencing_token": 11,
        }
    )
    snapshot = SnapshotRef.from_dict(
        {
            "snapshot_id": _uuid(1),
            "target_id": target.target_id,
            "epoch": target.epoch,
            "revision": target.revision,
            "fencing_token": target.fencing_token,
            "observed_at_ms": QUALIFICATION_NOW_MS,
            "sequence": 1,
        }
    )
    return _QualificationFixture(signer, policy, target, snapshot)


def _envelope(fixture: _QualificationFixture, serial: int) -> ControlEnvelope:
    if type(serial) is not int or not 0 <= serial < 100_000_000:
        raise ValueError("qualification_serial")
    request = ControlRequest.from_dict(
        {
            "schema_version": 1,
            "request_id": _uuid(100_000_000 + serial),
            "session_id": _uuid(2),
            "subject_id": "runtime.operator",
            "sequence": serial + 1,
            "issued_at_ms": QUALIFICATION_NOW_MS - 10,
            "deadline_ms": QUALIFICATION_NOW_MS + 20_000,
            "target": fixture.target.to_dict(),
            "snapshot": fixture.snapshot.to_dict(),
            "operation": Operation.ACTIVATE.value,
            "data_class": ControlDataClass.STRUCTURAL.value,
            "arguments": {"element_id": _opaque("henry-button")},
            "requested_routes": [ControlRoute.CONNECTOR.value],
            "max_output_bytes": 4096,
        }
    )
    grant = issue_grant(
        fixture.signer,
        fixture.policy,
        grant_id=_uuid(200_000_000 + serial),
        subject_id=request.subject_id,
        target_ids=(fixture.target.target_id,),
        target_kinds=(fixture.target.kind,),
        operations=(request.operation,),
        data_classes=(request.data_class,),
        routes=(ControlRoute.CONNECTOR,),
        issued_at_ms=QUALIFICATION_NOW_MS - 1_000,
        expires_at_ms=QUALIFICATION_NOW_MS + 30_000,
        maximum_action_count=1,
        max_input_bytes=fixture.policy.max_input_bytes,
        max_output_bytes=fixture.policy.max_output_bytes,
        max_transmit_bytes=0,
    )
    permit = issue_permit(
        fixture.signer,
        fixture.signer.verifier,
        fixture.policy,
        grant,
        request,
        permit_id=_uuid(300_000_000 + serial),
        issued_at_ms=QUALIFICATION_NOW_MS,
        expires_at_ms=QUALIFICATION_NOW_MS + 10_000,
    )
    return ControlEnvelope(request, grant, permit)


class _RaceAdapter:
    def __init__(self, snapshot: SnapshotRef) -> None:
        self.snapshot = snapshot
        self.snapshot_calls = 0
        self.dispatch_calls = 0
        self.mutation_count = 0

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]:
        del target
        return (ControlRoute.CONNECTOR,)

    def current_snapshot(self, target: TargetRef) -> SnapshotRef:
        del target
        self.snapshot_calls += 1
        if self.snapshot_calls == 1:
            return self.snapshot
        changed = self.snapshot.to_dict()
        changed["fencing_token"] += 1
        changed["sequence"] += 1
        changed["observed_at_ms"] += 1
        changed["snapshot_id"] = _uuid(4)
        return SnapshotRef.from_dict(changed)

    def dispatch(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterDispatchResult:
        del effect_id, request, route
        self.dispatch_calls += 1
        self.mutation_count += 1
        raise AssertionError("stale target reached dispatch")

    def reconcile(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterReconciliationResult:
        del request, route
        return AdapterReconciliationResult(
            ReconciliationDisposition.FAILED,
            "effect_absent",
            structural_evidence(effect_id, "effect_absent"),
        )


class _MutationAdapter:
    def __init__(self, snapshot: SnapshotRef, *, serial: int) -> None:
        self.snapshot = snapshot
        self.serial = serial
        self.dispatch_calls = 0
        self.reconcile_calls = 0
        self.mutation_count = 0
        self.effect_id = ""
        self.postcondition: SnapshotRef | None = None

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]:
        del target
        return (ControlRoute.CONNECTOR,)

    def current_snapshot(self, target: TargetRef) -> SnapshotRef:
        del target
        return self.snapshot

    def dispatch(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterDispatchResult:
        del route
        self.dispatch_calls += 1
        if self.effect_id and self.effect_id != effect_id:
            raise AssertionError("effect identity changed")
        self.effect_id = effect_id
        if self.mutation_count == 0:
            self.mutation_count = 1
            value = request.snapshot.to_dict()
            value["snapshot_id"] = _uuid(400_000_000 + self.serial)
            value["observed_at_ms"] += 1
            value["sequence"] += 1
            self.postcondition = SnapshotRef.from_dict(value)
        return AdapterDispatchResult(
            DispatchDisposition.APPLIED,
            "none",
            structural_evidence(effect_id, "dispatch_applied"),
        )

    def reconcile(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterReconciliationResult:
        del request, route
        self.reconcile_calls += 1
        if effect_id != self.effect_id or self.postcondition is None:
            return AdapterReconciliationResult(
                ReconciliationDisposition.UNKNOWN,
                "postcondition_missing",
            )
        reason = "reconciled_applied"
        return AdapterReconciliationResult(
            ReconciliationDisposition.VERIFIED,
            reason,
            fresh_postcondition_evidence(effect_id, reason, self.postcondition),
            self.postcondition,
        )


class _UnknownAdapter:
    def __init__(self, snapshot: SnapshotRef) -> None:
        self.snapshot = snapshot
        self.dispatch_calls = 0
        self.reconcile_calls = 0

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]:
        del target
        return (ControlRoute.CONNECTOR,)

    def current_snapshot(self, target: TargetRef) -> SnapshotRef:
        del target
        return self.snapshot

    def dispatch(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterDispatchResult:
        del request, route
        self.dispatch_calls += 1
        return AdapterDispatchResult(
            DispatchDisposition.UNKNOWN,
            "adapter_uncertain",
            structural_evidence(effect_id, "adapter_uncertain"),
        )

    def reconcile(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterReconciliationResult:
        del request, route
        self.reconcile_calls += 1
        return AdapterReconciliationResult(
            ReconciliationDisposition.UNKNOWN,
            "reconciliation_uncertain",
            structural_evidence(effect_id, "reconciliation_uncertain"),
        )


def _runtime(journal: ControlJournal, fixture: _QualificationFixture) -> ControlRuntime:
    return ControlRuntime(
        journal,
        fixture.signer.verifier,
        fixture.policy,
        fixture.signer,
        clock_ms=lambda: QUALIFICATION_NOW_MS + 1,
    )


def run_race_qualification(*, trials: int = MIN_RACE_TRIALS) -> HenryQualificationMetric:
    if type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("qualification_race_trials")
    fixture = _fixture()
    rejected = 0
    dispatches = 0
    mutations = 0
    durations: list[float] = []
    with tempfile.TemporaryDirectory(prefix="algo-henry-race-") as temporary:
        journal = ControlJournal(Path(temporary) / "ada-race.sqlite3")
        runtime = _runtime(journal, fixture)
        for serial in range(trials):
            adapter = _RaceAdapter(fixture.snapshot)
            started = time.perf_counter_ns()
            receipt = runtime.execute(_envelope(fixture, serial), adapter)
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            dispatches += adapter.dispatch_calls
            mutations += adapter.mutation_count
            if receipt.state is ControlEffectState.FAILED:
                rejected += 1
    passed = trials >= MIN_RACE_TRIALS and rejected == trials and dispatches == 0 and mutations == 0
    return HenryQualificationMetric(
        "stale_target_race",
        "pass" if passed else ("fail" if dispatches or mutations else "not_verified"),
        rejected,
        trials,
        f"0 mutations and 0 dispatches in at least {MIN_RACE_TRIALS} race-injected actions",
        "Deterministic claim-to-dispatch target-fence drift through the durable control runtime.",
        "Finite local adapters and SQLite do not prove browser, XPC, power-loss, or hostile-filesystem behavior.",
        {
            "dispatches": dispatches,
            "mutations": mutations,
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "seed": QUALIFICATION_SEED,
        },
    )


def run_postcondition_qualification(
    *,
    trials: int = MIN_POSTCONDITION_TRIALS,
) -> HenryQualificationMetric:
    if type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("qualification_postcondition_trials")
    fixture = _fixture()
    verified = 0
    mutations = 0
    durations: list[float] = []
    with tempfile.TemporaryDirectory(prefix="algo-henry-postcondition-") as temporary:
        journal = ControlJournal(Path(temporary) / "ada-postcondition.sqlite3")
        runtime = _runtime(journal, fixture)
        for serial in range(trials):
            adapter = _MutationAdapter(fixture.snapshot, serial=serial)
            started = time.perf_counter_ns()
            receipt = runtime.execute(_envelope(fixture, serial), adapter)
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            mutations += adapter.mutation_count
            if (
                receipt.state is ControlEffectState.VERIFIED
                and adapter.postcondition is not None
                and receipt.evidence_digest
                == fresh_postcondition_evidence(
                    receipt.effect_id,
                    "reconciled_applied",
                    adapter.postcondition,
                )
            ):
                verified += 1
    passed = trials >= MIN_POSTCONDITION_TRIALS and verified == trials and mutations == trials
    return HenryQualificationMetric(
        "fresh_postcondition",
        "pass" if passed else ("fail" if verified != trials or mutations != trials else "not_verified"),
        verified,
        trials,
        f"100% of at least {MIN_POSTCONDITION_TRIALS} successful mutations have a fresh bound postcondition",
        "Deterministic mutation, reconciliation, evidence binding, journal transition, and signed receipt path.",
        "Finite adapters do not establish that a future native or browser adapter observes a real external target correctly.",
        {
            "mutations": mutations,
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
        },
    )


def run_unknown_outcome_qualification(
    *,
    trials: int = MIN_UNKNOWN_OUTCOME_TRIALS,
) -> HenryQualificationMetric:
    if type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("qualification_unknown_trials")
    fixture = _fixture()
    held_unknown = 0
    dispatches = 0
    reconciliations = 0
    with tempfile.TemporaryDirectory(prefix="algo-henry-unknown-") as temporary:
        journal = ControlJournal(Path(temporary) / "ada-unknown.sqlite3")
        runtime = _runtime(journal, fixture)
        for serial in range(trials):
            adapter = _UnknownAdapter(fixture.snapshot)
            receipt = runtime.execute(_envelope(fixture, serial), adapter)
            dispatches += adapter.dispatch_calls
            reconciliations += adapter.reconcile_calls
            if receipt.state is ControlEffectState.UNKNOWN:
                held_unknown += 1
    extra_dispatches = max(0, dispatches - trials)
    passed = (
        trials >= MIN_UNKNOWN_OUTCOME_TRIALS
        and held_unknown == trials
        and dispatches == trials
        and reconciliations == 0
    )
    return HenryQualificationMetric(
        "unknown_outcome_no_retry",
        "pass" if passed else ("fail" if extra_dispatches or held_unknown != trials else "not_verified"),
        held_unknown,
        trials,
        f"0 automatic redispatches across at least {MIN_UNKNOWN_OUTCOME_TRIALS} unknown mutation outcomes",
        "Deterministic uncertain adapter outcomes through the durable control runtime.",
        "Explicit recovery is a separate reconciliation operation and remains covered by focused crash tests.",
        {
            "dispatches": dispatches,
            "extra_dispatches": extra_dispatches,
            "automatic_reconciliations": reconciliations,
        },
    )


def run_policy_qualification() -> HenryQualificationMetric:
    tool_names = set(tools.TOOL_MAP)
    policies = set(CURATED_TOOL_POLICIES)
    specs = tuple(spec for spec in action_registry.effective_action_specs() if spec.kind == "tool")
    privileged = tuple(
        (name, policy)
        for name, policy in CURATED_TOOL_POLICIES.items()
        if not policy.dynamic_resolution
        and (
            policy.effect_class is not EffectClass.OBSERVE
            or Capability.CREDENTIAL in policy.capabilities
            or Capability.DATA_EGRESS in policy.capabilities
        )
    )
    unconfirmed = sum(
        policy.confirmation_mode is ConfirmationMode.NONE
        for _name, policy in privileged
    )
    generated_privileged = sum(not spec.curated for spec in specs)
    hostile_unknowns = 1_000
    unknown_fail_closed = 0
    for index in range(hostile_unknowns):
        policy = policy_for_action(f"generated_privileged_{index}")
        if (
            not policy.curated
            and policy.effect_class is EffectClass.UNCLASSIFIED
            and policy.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED
            and Capability.UNCLASSIFIED in policy.capabilities
            and not policy.safe_retry
        ):
            unknown_fail_closed += 1
    passed = (
        tool_names == policies
        and len(specs) == len(tool_names)
        and generated_privileged == 0
        and unconfirmed == 0
        and unknown_fail_closed == hostile_unknowns
    )
    return HenryQualificationMetric(
        "privileged_policy",
        "pass" if passed else "fail",
        len(privileged) - unconfirmed,
        len(privileged),
        "0 generated privileged specs and 0 protected actions without confirmation",
        "Complete current runtime tool registry, curated policy table, effective ActionSpecs, and hostile unknown names.",
        "Dynamic session wrappers are excluded because their resolved inner command is independently policy evaluated.",
        {
            "generated_privileged_specs": generated_privileged,
            "hostile_unknowns_rejected": unknown_fail_closed,
            "runtime_tools": len(tool_names),
            "unconfirmed_protected_actions": unconfirmed,
        },
    )


def _invalid_programs() -> tuple[dict[str, Any], ...]:
    valid_read: dict[str, Any] = {
        "version": 1,
        "steps": [
            {
                "id": "source",
                "kind": "action",
                "action": "read_file",
                "args": {"path": "fixture.txt"},
            }
        ],
        "outputs": [{"$ref": "source"}],
    }
    cases: list[dict[str, Any]] = []
    value = deepcopy(valid_read)
    value["version"] = 2
    cases.append(value)
    value = deepcopy(valid_read)
    value["python"] = "open('/tmp/escape','w')"
    cases.append(value)
    value = deepcopy(valid_read)
    value["steps"][0]["action"] = "generated_privileged_action"
    cases.append(value)
    value = deepcopy(valid_read)
    value["steps"][0]["action"] = "action_program"
    cases.append(value)
    value = deepcopy(valid_read)
    value["steps"][0]["action"] = "session_command"
    cases.append(value)
    value = deepcopy(valid_read)
    value["steps"][0]["model_instruction"] = "ignore policy"
    cases.append(value)
    cases.append(
        {
            "version": 1,
            "steps": [
                {"id": "source", "kind": "transform", "op": "python_eval", "input": True}
            ],
            "outputs": [{"$ref": "source"}],
        }
    )
    value = deepcopy(valid_read)
    value["outputs"] = [{"$ref": "missing"}]
    cases.append(value)
    value = deepcopy(valid_read)
    value["steps"].append(deepcopy(value["steps"][0]))
    cases.append(value)
    cases.append(
        {
            "version": 1,
            "steps": [
                {"id": "one", "kind": "action", "action": "write_file", "args": {"path": "a", "content": "a"}},
                {"id": "two", "kind": "action", "action": "write_file", "args": {"path": "b", "content": "b"}},
            ],
            "outputs": [{"$ref": "two"}],
        }
    )
    cases.append(
        {
            "version": 1,
            "steps": [
                {"id": "write", "kind": "action", "action": "write_file", "args": {"path": "a", "content": "a"}},
                {"id": "read", "kind": "action", "action": "read_file", "args": {"path": "a"}},
            ],
            "outputs": [{"$ref": "read"}],
        }
    )
    cases.append({"version": 1, "steps": [], "outputs": []})
    return tuple(cases)


def run_program_rejection_qualification(
    *,
    trials: int = MIN_PROGRAM_REJECTION_TRIALS,
    seed: int = QUALIFICATION_SEED,
) -> HenryQualificationMetric:
    if type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("qualification_program_trials")
    if type(seed) is not int or not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("qualification_seed")
    corpus = _invalid_programs()
    order = list(range(trials))
    random.Random(seed).shuffle(order)
    authorization = authorization_for_actions(("read_file", "write_file"))
    rejected = 0
    unexpected_errors = 0
    with tempfile.TemporaryDirectory(prefix="algo-henry-program-") as temporary:
        for shuffled in order:
            plan = deepcopy(corpus[shuffled % len(corpus)])
            try:
                compile_program(
                    plan,
                    authorization=authorization,
                    cwd=temporary,
                    safe_mode=True,
                )
            except ProgramValidationError:
                rejected += 1
            except Exception:
                unexpected_errors += 1
    passed = (
        trials >= MIN_PROGRAM_REJECTION_TRIALS
        and rejected == trials
        and unexpected_errors == 0
    )
    return HenryQualificationMetric(
        "arbitrary_program_rejection",
        "pass" if passed else ("fail" if rejected != trials or unexpected_errors else "not_verified"),
        rejected,
        trials,
        f"0 arbitrary program accepts across at least {MIN_PROGRAM_REJECTION_TRIALS} randomized malformed plans",
        "Closed typed-program root, action, transform, reference, mutation-finality, and capability-ceiling schemas.",
        "The corpus is deterministic and finite; the 100,000-frame protocol fuzzer covers byte-level parser faults separately.",
        {
            "corpus_cases": len(corpus),
            "seed": seed,
            "unexpected_errors": unexpected_errors,
        },
    )


def run_privacy_qualification(
    *,
    trials: int = MIN_PRIVACY_CANARIES,
) -> HenryQualificationMetric:
    if type(trials) is not int or not 1 <= trials <= 100_000:
        raise ValueError("qualification_privacy_trials")
    clean = 0
    for index in range(trials):
        canary = "algo-private-" + hashlib.sha256(f"canary-{index}".encode("ascii")).hexdigest()
        arguments = {
            "access_token": canary,
            "nested": {"password": canary, "content": canary},
            "url": f"https://example.invalid/path?token={canary}",
            "selector": f"#{canary}",
        }
        rendered = json.dumps(
            {
                "audit": project_action_args(
                    "future_browser_action",
                    arguments,
                    PrivacyView.AUDIT,
                    hmac_key=b"h" * 32,
                ),
                "telemetry": project_action_args(
                    "future_browser_action",
                    arguments,
                    PrivacyView.TELEMETRY,
                    hmac_key=b"h" * 32,
                ),
            },
            sort_keys=True,
        )
        if canary not in rendered:
            clean += 1
    passed = trials >= MIN_PRIVACY_CANARIES and clean == trials
    return HenryQualificationMetric(
        "privacy_canaries",
        "pass" if passed else ("fail" if clean != trials else "not_verified"),
        clean,
        trials,
        f"0 raw canaries in at least {MIN_PRIVACY_CANARIES} audit and telemetry projections",
        "Nested credentials, content, URLs, selectors, and adversarially unique canaries.",
        "Focused tests cover logs, receipts, encrypted artifacts, and memory; live browser/native surfaces remain unavailable.",
        {"projection_views": 2},
    )


def protocol_metric(report: Mapping[str, Any]) -> HenryQualificationMetric:
    required = {
        "iterations",
        "rejected",
        "unexpected_accepts",
        "unexpected_crashes",
        "maximum_case_bytes",
        "maximum_buffered_bytes",
        "corpus_digest",
        "classification_digest",
        "passed",
    }
    if not required <= set(report):
        raise ValueError("qualification_protocol_report")
    iterations = report["iterations"]
    rejected = report["rejected"]
    accepts = report["unexpected_accepts"]
    crashes = report["unexpected_crashes"]
    if not all(type(value) is int for value in (iterations, rejected, accepts, crashes)):
        raise ValueError("qualification_protocol_report")
    passed = (
        iterations >= MIN_PROTOCOL_FRAMES
        and rejected == iterations
        and accepts == 0
        and crashes == 0
        and report["passed"] is True
    )
    return HenryQualificationMetric(
        "malformed_protocol_frames",
        "pass" if passed else ("fail" if accepts or crashes else "not_verified"),
        rejected,
        iterations,
        f"0 crashes, OOMs, or unexpected accepts in at least {MIN_PROTOCOL_FRAMES} malformed frames",
        "Bounded fragmented David control frames across 25 mutation classes, including prompt-like extra fields.",
        "In-process deterministic fuzzing is not a native-host, XPC, browser-process, or memory-sanitizer run.",
        {
            "classification_digest": str(report["classification_digest"]),
            "corpus_digest": str(report["corpus_digest"]),
            "maximum_buffered_bytes": int(report["maximum_buffered_bytes"]),
            "unexpected_accepts": accepts,
            "unexpected_crashes": crashes,
        },
    )


def efficiency_metric(report: Mapping[str, Any]) -> HenryQualificationMetric:
    try:
        assert_tool_context_efficiency(dict(report))
    except (AssertionError, KeyError, TypeError, ValueError):
        passed = False
    else:
        passed = True
    repeats = report.get("repeats")
    if type(repeats) is not int:
        repeats = 0
    raw_summary = report.get("summary")
    summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
    raw_typed = report.get("typed_program")
    typed: Mapping[str, Any] = raw_typed if isinstance(raw_typed, Mapping) else {}
    token_reduction = float(typed.get("reduction_pct") or 0.0)
    passed = passed and repeats >= MIN_EFFICIENCY_REPETITIONS and token_reduction >= 50.0
    return HenryQualificationMetric(
        "local_token_efficiency",
        "pass" if passed else "not_verified",
        repeats if passed else (0 if repeats > 0 else None),
        repeats if repeats > 0 else None,
        f"at least {MIN_EFFICIENCY_REPETITIONS} repetitions and at least 50% token reduction",
        "Model-free deferred-tool, semantic-supersession, and artifact-backed typed-program cells.",
        "Synthetic local token estimates do not establish task completion, model quality, screenshot reduction, or cross-harness advantage.",
        {
            "median_schema_reduction_pct": float(summary.get("median_schema_reduction_pct") or 0.0),
            "typed_program_reduction_pct": token_reduction,
        },
    )


def blocked_metric(metric_id: str, threshold: str, scope: str, limitations: str) -> HenryQualificationMetric:
    return HenryQualificationMetric(
        metric_id,
        "blocked",
        None,
        None,
        threshold,
        scope,
        limitations,
        {},
    )


def build_qualification_report(
    *,
    protocol_report: Mapping[str, Any],
    efficiency_report: Mapping[str, Any],
    focused_suite_passed: bool,
    source_digest: str,
    race_trials: int = MIN_RACE_TRIALS,
    postcondition_trials: int = MIN_POSTCONDITION_TRIALS,
    unknown_trials: int = MIN_UNKNOWN_OUTCOME_TRIALS,
    program_trials: int = MIN_PROGRAM_REJECTION_TRIALS,
    privacy_trials: int = MIN_PRIVACY_CANARIES,
    generated_at: str,
) -> dict[str, Any]:
    if not source_digest.startswith(_DIGEST_PREFIX) or len(source_digest) != 71:
        raise ValueError("qualification_source_digest")
    if not generated_at.endswith("Z"):
        raise ValueError("qualification_timestamp")
    local_metrics = [
        run_policy_qualification(),
        run_race_qualification(trials=race_trials),
        run_unknown_outcome_qualification(trials=unknown_trials),
        run_program_rejection_qualification(trials=program_trials),
        run_privacy_qualification(trials=privacy_trials),
        protocol_metric(protocol_report),
        run_postcondition_qualification(trials=postcondition_trials),
        efficiency_metric(efficiency_report),
        HenryQualificationMetric(
            "focused_adversarial_suite",
            "pass" if focused_suite_passed else "fail",
            1 if focused_suite_passed else 0,
            1,
            "focused race, crash, policy, privacy, program, browser, and network contract suite passes",
            "Current Python import under the qualification environment and source-owned hardening tests.",
            "Passing deterministic tests cannot substitute for live production browser, extension, native signing, or TCC evidence.",
            {},
        ),
    ]
    blocked = [
        blocked_metric(
            "managed_browser_completion",
            "at least 95% supported-task completion over at least five cold/warm rotated repetitions",
            "Production managed-browser task matrix with frozen fixtures and independent checkers.",
            "A current image is locally attested, but no native-platform live broker session or repeated task matrix satisfies M5.",
        ),
        blocked_metric(
            "selected_chrome_completion",
            "at least 90% supported-task completion over at least five cold/warm rotated repetitions",
            "Installed selected-tab Chrome extension/native-host task matrix.",
            "The observe-only package is not installed, paired, connected, or live-granted.",
        ),
        blocked_metric(
            "semantic_and_screenshot_efficiency",
            "at least 90% semantic routes and at least 70% screenshot reduction versus screenshot-only",
            "Matched live task cells with action, screenshot, confirmation, token, and policy counts.",
            "No production browser or signed native task runner is available for a fair matched baseline.",
        ),
        blocked_metric(
            "browser_profile_network_boundary",
            "0 default-profile, cookie, storage, password, incognito, internal-page, or private-network accesses",
            "Live production browser, broker, selected-tab, redirect, DNS, and peer-pin fault matrix.",
            "Contracts and a probe container pass, but the exact public-browser topology remains unproven.",
        ),
        blocked_metric(
            "browser_security_freshness",
            "browser security update lag no more than 72 hours",
            "Exact pinned production version versus a current authoritative stable-release observation.",
            "The matching Linux image has local zero-lag evidence, but this report has no current hosted native-platform or registry-provenance result.",
        ),
    ]
    metrics = [*local_metrics, *blocked]
    statuses = {metric.status for metric in metrics}
    overall = "fail" if "fail" in statuses else "blocked" if "blocked" in statuses else "pass"
    fixture = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "seed": QUALIFICATION_SEED,
        "race_trials": race_trials,
        "postcondition_trials": postcondition_trials,
        "unknown_trials": unknown_trials,
        "program_trials": program_trials,
        "privacy_trials": privacy_trials,
        "protocol_frames": int(protocol_report["iterations"]),
        "efficiency_repetitions": int(efficiency_report.get("repeats") or 0),
    }
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "qualification": "henry-m8-local-v1",
        "status": overall,
        "public_claim_eligible": False,
        "generated_at": generated_at,
        "source_digest": source_digest,
        "fixture_digest": _digest(fixture),
        "fixture": fixture,
        "metrics": [metric.to_dict() for metric in metrics],
        "summary": {
            "pass": sum(metric.status == "pass" for metric in metrics),
            "fail": sum(metric.status == "fail" for metric in metrics),
            "blocked": sum(metric.status == "blocked" for metric in metrics),
            "not_verified": sum(metric.status == "not_verified" for metric in metrics),
        },
        "limitations": [
            "Zero observed failures in a finite local sample is not zero risk.",
            "No production browser, selected-Chrome, Developer ID, notarization, Gatekeeper, or live TCC claim is made.",
            "The active hardening freeze remains in force.",
        ],
    }


__all__ = [
    "HenryQualificationMetric",
    "MIN_POSTCONDITION_TRIALS",
    "MIN_PRIVACY_CANARIES",
    "MIN_PROGRAM_REJECTION_TRIALS",
    "MIN_PROTOCOL_FRAMES",
    "MIN_RACE_TRIALS",
    "MIN_UNKNOWN_OUTCOME_TRIALS",
    "QUALIFICATION_SCHEMA_VERSION",
    "build_qualification_report",
    "efficiency_metric",
    "protocol_metric",
    "run_policy_qualification",
    "run_postcondition_qualification",
    "run_privacy_qualification",
    "run_program_rejection_qualification",
    "run_race_qualification",
    "run_unknown_outcome_qualification",
    "wilson_interval",
]
