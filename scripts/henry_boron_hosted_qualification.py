#!/usr/bin/env python3
"""Run repeated source-bound Boron sessions on a native hosted amd64 runner."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Callable, Mapping, NoReturn

from algo_cli.boron_browser_entry import BoronEntryRejected
from algo_cli.boron_browser_isolation import (
    BORON_MAX_SECURITY_LAG_MS,
    BoronIsolationRejected,
)
from algo_cli.boron_browser_wrapper import BoronPipeRejected
from algo_cli.xenon_browser_broker import XenonBrokerRejected
from algo_cli.xenon_browser_entry import XenonEntryRejected
from boron_browser_build_images import BuildRejected, CHROME_VERSION, build_images
from boron_browser_live_session import (
    LiveSessionRejected,
    _validated_build_evidence,
    run_live_session,
)


ROOT = Path(__file__).resolve().parents[1]
MIN_REPETITIONS = 5
MAX_REPETITIONS = 20
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_DURATION_MS = 10 * 60 * 1000
HOSTED_LIMITATION = (
    "Repeated isolated public GET evidence only; it does not prove broad-site "
    "compatibility, selected-Chrome behavior, supported-task completion, model "
    "quality, token or screenshot reduction, interactive macOS permissions, or "
    "product readiness."
)
SOURCE_PATHS = (
    ".github/workflows/oliver-ci.yml",
    "algo_cli/boron_browser_entry.py",
    "algo_cli/boron_browser_isolation.py",
    "algo_cli/boron_browser_wrapper.py",
    "algo_cli/xenon_browser_broker.py",
    "algo_cli/xenon_browser_egress.py",
    "algo_cli/xenon_browser_entry.py",
    "algo_cli/resources/boron_browser/boron_browser_wrapper.sh",
    "algo_cli/resources/boron_browser/boron_managed_policy.json",
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
    "algo_cli/resources/boron_browser/boron_seccomp_profile.json",
    "algo_cli/resources/boron_browser/xenon_egress_broker.sh",
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile",
    "scripts/boron_browser_build_images.py",
    "scripts/boron_browser_live_session.py",
    "scripts/henry_boron_hosted_qualification.py",
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_INTEGER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LIVE_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "platform",
        "browser_image_digest",
        "broker_image_digest",
        "broker_binary_digest",
        "topology_evidence_digest",
        "internal_participant_count",
        "browser_state",
        "browser_major",
        "browser_security_update_lag_ms",
        "browser_security_source_digest",
        "browser_command_count",
        "browser_event_count",
        "broker_disposition",
        "broker_connection_count",
        "broker_request_count",
        "broker_redirect_count",
        "broker_bytes_to_browser",
        "target_decision_digest",
        "ca_certificate_digest",
        "browser_stderr",
        "broker_stderr",
    }
)
class HostedQualificationRejected(RuntimeError):
    """A content-free hosted qualification invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "hosted_qualification_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise HostedQualificationRejected(reason_code)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        _reject("hosted_evidence_json")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _percentile(values: list[int], percentile: float) -> int:
    if not values or not 0.0 <= percentile <= 1.0:
        _reject("hosted_duration")
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)),
    )
    return ordered[index]


def _wilson_interval(successes: int, trials: int) -> list[float]:
    if (
        type(successes) is not int
        or type(trials) is not int
        or not 0 <= successes <= trials
        or trials < 1
    ):
        _reject("hosted_denominator")
    z = 1.959963984540054
    proportion = successes / trials
    denominator = 1.0 + (z * z / trials)
    center = (proportion + (z * z / (2.0 * trials))) / denominator
    margin = (
        z
        * (
            (proportion * (1.0 - proportion) / trials)
            + (z * z / (4.0 * trials * trials))
        )
        ** 0.5
        / denominator
    )
    return [
        round(max(0.0, center - margin), 6),
        round(min(1.0, center + margin), 6),
    ]


def _source_digest() -> str:
    digest = hashlib.sha256()
    for relative in sorted(SOURCE_PATHS):
        path = ROOT / relative
        try:
            info = path.lstat()
        except OSError:
            _reject("hosted_source_identity")
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or not 1 <= info.st_size <= MAX_SOURCE_BYTES
        ):
            _reject("hosted_source_identity")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (info.st_dev, info.st_ino, info.st_size)
            ):
                _reject("hosted_source_changed")
            payload = bytearray()
            while len(payload) < opened.st_size:
                chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
                if not chunk:
                    _reject("hosted_source_changed")
                payload.extend(chunk)
            if os.read(descriptor, 1):
                _reject("hosted_source_changed")
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                _reject("hosted_source_changed")
        except OSError:
            _reject("hosted_source_identity")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _bounded_text(value: Any, reason_code: str, *, maximum: int = 512) -> str:
    if type(value) is not str or _CONTROL_RE.search(value) is not None:
        _reject(reason_code)
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        _reject(reason_code)
    if not 1 <= size <= maximum:
        _reject(reason_code)
    return value


def _positive_integer(value: Any, reason_code: str) -> int:
    if type(value) is not str or _INTEGER_RE.fullmatch(value) is None:
        _reject(reason_code)
    parsed = int(value)
    if not 1 <= parsed <= (1 << 53) - 1:
        _reject(reason_code)
    return parsed


@dataclass(frozen=True, slots=True)
class HostedRunnerContext:
    source_revision: str
    repository_id: int
    run_id: int
    run_attempt: int
    event_name: str
    workflow_ref_digest: str
    runner_os: str
    runner_arch: str
    native_platform: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "HostedRunnerContext":
        if type(environment) is not dict or environment.get("GITHUB_ACTIONS") != "true":
            _reject("hosted_environment")
        revision = environment.get("GITHUB_SHA")
        if type(revision) is not str or _REVISION_RE.fullmatch(revision) is None:
            _reject("hosted_revision")
        runner_os = environment.get("RUNNER_OS")
        runner_arch = environment.get("RUNNER_ARCH")
        if runner_os != "Linux" or runner_arch != "X64":
            _reject("hosted_native_amd64_required")
        event_name = environment.get("GITHUB_EVENT_NAME")
        if event_name not in {"push", "pull_request", "workflow_dispatch"}:
            _reject("hosted_event")
        workflow_ref = _bounded_text(
            environment.get("GITHUB_WORKFLOW_REF"),
            "hosted_workflow_ref",
        )
        return cls(
            source_revision=revision,
            repository_id=_positive_integer(
                environment.get("GITHUB_REPOSITORY_ID"),
                "hosted_repository_id",
            ),
            run_id=_positive_integer(environment.get("GITHUB_RUN_ID"), "hosted_run_id"),
            run_attempt=_positive_integer(
                environment.get("GITHUB_RUN_ATTEMPT"),
                "hosted_run_attempt",
            ),
            event_name=event_name,
            workflow_ref_digest="sha256:"
            + hashlib.sha256(workflow_ref.encode("utf-8")).hexdigest(),
            runner_os=runner_os,
            runner_arch=runner_arch,
            native_platform="linux/amd64",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "native_platform": self.native_platform,
            "repository_id": self.repository_id,
            "run_attempt": self.run_attempt,
            "run_id": self.run_id,
            "runner_arch": self.runner_arch,
            "runner_os": self.runner_os,
            "source_revision": self.source_revision,
            "workflow_ref_digest": self.workflow_ref_digest,
        }


def _stderr_evidence(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"byte_count", "digest"}:
        _reject("hosted_stderr_evidence")
    byte_count = value["byte_count"]
    digest = value["digest"]
    if (
        type(byte_count) is not int
        or not 0 <= byte_count <= 1_048_576
        or type(digest) is not str
        or _DIGEST_RE.fullmatch(digest) is None
    ):
        _reject("hosted_stderr_evidence")
    return {"byte_count": byte_count, "digest": digest}


def _validated_live_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _LIVE_EVIDENCE_KEYS:
        _reject("hosted_live_evidence_shape")
    evidence = dict(value)
    if (
        evidence["schema_version"] != 1
        or evidence["platform"] != "linux/amd64"
        or evidence["internal_participant_count"] != 2
        or evidence["browser_state"] != "verified"
        or evidence["broker_disposition"] != "verified"
    ):
        _reject("hosted_live_evidence_identity")
    for field in (
        "browser_image_digest",
        "broker_image_digest",
        "broker_binary_digest",
        "topology_evidence_digest",
        "browser_security_source_digest",
        "target_decision_digest",
        "ca_certificate_digest",
    ):
        if type(evidence[field]) is not str or _DIGEST_RE.fullmatch(evidence[field]) is None:
            _reject("hosted_live_evidence_digest")
    exact_positive = (
        "browser_major",
        "browser_command_count",
        "browser_event_count",
        "broker_connection_count",
        "broker_request_count",
        "broker_bytes_to_browser",
    )
    if any(
        type(evidence[field]) is not int
        or not 1 <= evidence[field] <= (1 << 53) - 1
        for field in exact_positive
    ):
        _reject("hosted_live_evidence_count")
    if evidence["browser_major"] != int(CHROME_VERSION.split(".", 1)[0]):
        _reject("hosted_browser_major")
    lag = evidence["browser_security_update_lag_ms"]
    redirects = evidence["broker_redirect_count"]
    if (
        type(lag) is not int
        or not 0 <= lag <= BORON_MAX_SECURITY_LAG_MS
        or type(redirects) is not int
        or not 0 <= redirects <= 2
    ):
        _reject("hosted_live_evidence_count")
    evidence["browser_stderr"] = _stderr_evidence(evidence["browser_stderr"])
    evidence["broker_stderr"] = _stderr_evidence(evidence["broker_stderr"])
    return evidence


def _assert_build_live_binding(
    build: Mapping[str, Any],
    live: Mapping[str, Any],
) -> None:
    """Cross-bind each live result to the one source-attested image build."""

    if type(build) is not dict or type(live) is not dict:
        _reject("hosted_build_live_binding")
    bindings = (
        ("browser_image_id", "browser_image_digest", "hosted_browser_build_binding"),
        ("broker_image_id", "broker_image_digest", "hosted_broker_build_binding"),
        ("broker_code_digest", "broker_binary_digest", "hosted_broker_binary_binding"),
        (
            "browser_security_source_digest",
            "browser_security_source_digest",
            "hosted_release_evidence_binding",
        ),
    )
    for build_field, live_field, reason_code in bindings:
        if build.get(build_field) != live.get(live_field):
            _reject(reason_code)


def build_hosted_report(
    *,
    context: HostedRunnerContext,
    build_evidence: Mapping[str, Any],
    repetitions: list[tuple[Mapping[str, Any], int]],
    generated_at: str,
    source_digest: str,
) -> dict[str, Any]:
    if type(context) is not HostedRunnerContext:
        _reject("hosted_context")
    build = _validated_build_evidence(build_evidence)
    if not MIN_REPETITIONS <= len(repetitions) <= MAX_REPETITIONS:
        _reject("hosted_repetitions")
    if (
        type(generated_at) is not str
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", generated_at)
        is None
        or type(source_digest) is not str
        or _DIGEST_RE.fullmatch(source_digest) is None
    ):
        _reject("hosted_report_identity")

    rows: list[dict[str, Any]] = []
    durations: list[int] = []
    topology_digests: set[str] = set()
    browser_image_digest = ""
    broker_image_digest = ""
    for index, (raw, duration_ms) in enumerate(repetitions, start=1):
        if type(duration_ms) is not int or not 1 <= duration_ms <= MAX_DURATION_MS:
            _reject("hosted_duration")
        evidence = _validated_live_evidence(raw)
        _assert_build_live_binding(build, evidence)
        if index == 1:
            browser_image_digest = evidence["browser_image_digest"]
            broker_image_digest = evidence["broker_image_digest"]
        elif (
            evidence["browser_image_digest"] != browser_image_digest
            or evidence["broker_image_digest"] != broker_image_digest
        ):
            _reject("hosted_image_changed")
        topology = evidence["topology_evidence_digest"]
        if topology in topology_digests:
            _reject("hosted_topology_reused")
        topology_digests.add(topology)
        durations.append(duration_ms)
        rows.append(
            {
                "duration_ms": duration_ms,
                "evidence": evidence,
                "evidence_digest": _digest(evidence),
                "repetition": index,
                "session_state": "fresh_ephemeral",
            }
        )

    summary = {
        "completed": len(rows),
        "denominator": len(rows),
        "duration_p50_ms": _percentile(durations, 0.50),
        "duration_p95_ms": _percentile(durations, 0.95),
        "maximum_security_update_lag_ms": max(
            row["evidence"]["browser_security_update_lag_ms"] for row in rows
        ),
        "native_amd64": True,
        "rate": 1.0,
        "unique_ephemeral_topologies": len(topology_digests),
        "wilson_95": _wilson_interval(len(rows), len(rows)),
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "public_claim_eligible": False,
        "generated_at": generated_at,
        "source_digest": source_digest,
        "runner": context.to_dict(),
        "build_evidence": build,
        "build_evidence_digest": _digest(build),
        "repetitions": rows,
        "summary": summary,
        "supports": ["HARD-050"],
        "limitation": HOSTED_LIMITATION,
    }
    report["evidence_digest"] = _digest(report)
    return report


def run_hosted_qualification(
    *,
    environment: Mapping[str, str],
    repetitions: int,
    build: Callable[[], Mapping[str, Any]] = build_images,
    session: Callable[..., Mapping[str, Any]] = run_live_session,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    if type(repetitions) is not int or not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        _reject("hosted_repetitions")
    context = HostedRunnerContext.from_environment(environment)
    build_evidence = _validated_build_evidence(build())
    results: list[tuple[Mapping[str, Any], int]] = []
    for _index in range(repetitions):
        started = monotonic_ns()
        evidence = _validated_live_evidence(
            session(build_evidence=build_evidence)
        )
        finished = monotonic_ns()
        if type(started) is not int or type(finished) is not int or finished <= started:
            _reject("hosted_clock")
        duration_ms = max(1, (finished - started + 999_999) // 1_000_000)
        results.append((evidence, duration_ms))
    observed = now()
    if type(observed) is not datetime or observed.tzinfo is None:
        _reject("hosted_clock")
    generated_at = (
        observed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return build_hosted_report(
        context=context,
        build_evidence=build_evidence,
        repetitions=results,
        generated_at=generated_at,
        source_digest=_source_digest(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=MIN_REPETITIONS)
    arguments = parser.parse_args(argv)
    known_rejections = (
        HostedQualificationRejected,
        BuildRejected,
        LiveSessionRejected,
        BoronIsolationRejected,
        BoronEntryRejected,
        BoronPipeRejected,
        XenonBrokerRejected,
        XenonEntryRejected,
    )
    try:
        report = run_hosted_qualification(
            environment=dict(os.environ),
            repetitions=arguments.repetitions,
        )
    except known_rejections as error:
        reason_code = getattr(error, "reason_code", str(error))
        print(
            json.dumps(
                {"reason_code": reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"reason_code": "hosted_internal_error", "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
