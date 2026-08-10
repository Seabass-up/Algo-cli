#!/usr/bin/env python3
"""Run one real, isolated Chrome navigation through the Xenon broker."""

from __future__ import annotations

import base64
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, IO, Iterable, Mapping, NoReturn

from algo_cli.boron_browser_entry import (
    BORON_ENTRY_PROTOCOL_VERSION,
    BORON_ENTRY_SCHEMA_VERSION,
    BoronEntryRejected,
    BoronStartConfig,
)
from algo_cli.boron_browser_isolation import (
    BORON_MAX_SECURITY_LAG_MS,
    BoronBrowserFamily,
    BoronBrowserReleaseEvidence,
    BoronBrowserLaunch,
    BoronBrokerImagePin,
    BoronBrokerLaunch,
    BoronImagePin,
    BoronImagePurpose,
    BoronIsolationRejected,
    BoronNetworkPlan,
    BoronReleaseEvidenceSource,
    verify_docker_topology,
)
from algo_cli.boron_browser_wrapper import BoronPipeRejected, decode_boron_pipe_message
from algo_cli.xenon_browser_broker import (
    XENON_BROKER_PROTOCOL_VERSION,
    XENON_BROKER_SCHEMA_VERSION,
    XenonBrokerRejected,
    issue_xenon_broker_permit,
)
from algo_cli.xenon_browser_entry import XenonEntryRejected, read_xenon_entry_frame
from boron_browser_build_images import (
    CHROME_RELEASE_AT_MS,
    CHROME_VERSION,
    CRYPTOGRAPHY_VERSION,
    PLATFORM,
    BuildRejected,
    hosted_registry_tags,
)


ROOT = Path(__file__).resolve().parents[1]
SECCOMP = ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json"
TARGET_URL = "https://example.com/"
MAX_CONTROL_FRAME_BYTES = 131_072
MAX_STDERR_EVIDENCE_BYTES = 1_048_576
MAX_SECCOMP_PROFILE_BYTES = 131_072
DRIVER_SHUTDOWN_TIMEOUT_SECONDS = 3.0
CLEANUP_ABSENCE_TIMEOUT_SECONDS = 3.0
CLEANUP_INSPECT_TIMEOUT_SECONDS = 2.0
CLEANUP_MUTATION_TIMEOUT_SECONDS = 5.0
MAX_CLEANUP_INSPECT_BYTES = 16_384
LIVE_EVIDENCE_LIMITATION = (
    "One live public GET on native amd64 Linux Docker; not product readiness or broad-site compatibility."
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY_RE = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]{0,38})/"
    r"[a-z0-9](?:[a-z0-9._-]{0,126})$"
)
_REGISTRY_TAG_RE = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]{0,38})/"
    r"[a-z0-9](?:[a-z0-9._-]{0,126})"
    r":run-[1-9][0-9]{0,19}-[1-9][0-9]{0,19}-[0-9a-f]{40}$"
)
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_PRIMARY_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_DOCKER_RESOURCE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_RESOURCE_KEYS = frozenset(
    {
        "browser_container",
        "broker_container",
        "egress_network",
        "internal_network",
    }
)
_BROWSER_TERMINAL_REASONS = frozenset(
    {
        "cdp_command_failed",
        "dialog_handoff",
        "download_denied",
        "frame_drift",
        "lifecycle_drift",
        "loader_drift",
        "navigation_failed",
        "origin_drift",
        "popup_handoff",
        "target_crashed",
        "target_detached",
        "top_frame_detached",
        "unexpected_target",
        "upload_handoff",
        "websocket_denied",
    }
)
_LIVE_STATIC_REASON_CODES = frozenset(
    {
        "browser_build_evidence_digest",
        "browser_build_evidence_identity",
        "browser_build_evidence_shape",
        "browser_build_evidence_time",
        "browser_build_evidence_version",
        "browser_ca_identity",
        "browser_entry_rejected",
        "browser_error_shape",
        "browser_not_verified",
        "browser_result_type",
        "browser_security_evidence_shape",
        "browser_terminal_rejected",
        "broker_ready_identity",
        "broker_result_invariant",
        "broker_stop_failed",
        "cleanup_incomplete",
        "container_inspect_timeout",
        "container_inspect_unavailable",
        "control_frame_json",
        "control_frame_size",
        "control_frame_write",
        "control_input_close",
        "control_input_finished",
        "control_stderr_evidence_incomplete",
        "control_stderr_size",
        "host_dns_failed",
        "live_broker_code_changed",
        "live_broker_image_changed",
        "live_browser_image_changed",
        "live_build_evidence_required",
        "live_build_image_binding",
        "live_failure_and_cleanup_incomplete",
        "live_internal_error",
        "live_platform_emulation_forbidden",
        "live_registry_run_binding",
        "live_seccomp_close_failed",
        "live_seccomp_profile",
        "live_seccomp_profile_required",
        "live_seccomp_unavailable",
        "registry_identity",
        "registry_identity_json",
        "registry_identity_mismatch",
    }
)
_LIVE_RUN_STAGES = frozenset(
    {
        "broker_egress_connect",
        "docker_platform",
        "egress_network_create",
        "egress_network_inspect",
        "internal_network_create",
        "internal_network_inspect",
        "registry_identity",
    }
)
_LIVE_DRIVER_STAGES = frozenset({"broker_start", "browser_start"})
_LIVE_READ_STAGES = frozenset({"broker_ready", "broker_result", "browser_result"})
_LIVE_WAIT_STAGES = frozenset({"broker_attach_exit", "browser_exit"})
_LIVE_BASE_REASON_CODES = frozenset(
    _LIVE_STATIC_REASON_CODES
    | {"browser_" + reason for reason in _BROWSER_TERMINAL_REASONS}
    | {stage + suffix for stage in _LIVE_RUN_STAGES for suffix in ("_failed", "_unavailable")}
    | {stage + suffix for stage in _LIVE_DRIVER_STAGES for suffix in ("_pipes", "_setup_failed", "_unavailable")}
    | {
        stage + suffix
        for stage in _LIVE_READ_STAGES
        for suffix in ("_empty", "_eof", "_exit", "_read", "_size", "_timeout")
    }
    | {stage + suffix for stage in _LIVE_WAIT_STAGES for suffix in ("_failed", "_stderr_drain_timeout", "_timeout")}
)
_BUILD_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "platform",
        "qualification_source_digest",
        "browser_tag",
        "browser_repository",
        "browser_index_digest",
        "browser_platform_manifest_digest",
        "browser_config_digest",
        "browser_build_metadata_digest",
        "browser_provenance_digest",
        "browser_sbom_digest",
        "browser_code_digest",
        "browser_version",
        "browser_security_update_lag_ms",
        "browser_security_max_update_lag_ms",
        "browser_security_latest_version",
        "browser_security_latest_release_at_ms",
        "browser_security_evidence_observed_at_ms",
        "browser_security_source",
        "browser_security_source_digest",
        "native_browser_built",
        "native_browser_fresh",
        "native_browser_freshness_reason",
        "broker_tag",
        "broker_repository",
        "broker_index_digest",
        "broker_platform_manifest_digest",
        "broker_config_digest",
        "broker_build_metadata_digest",
        "broker_provenance_digest",
        "broker_sbom_digest",
        "broker_code_digest",
        "cryptography_version",
        "image_provenance",
        "non_root_defaults",
    }
)


def _normalized_live_reason(reason_code: Any) -> str:
    if type(reason_code) is not str or _REASON_CODE_RE.fullmatch(reason_code) is None:
        return "live_internal_error"
    if reason_code in _LIVE_BASE_REASON_CODES:
        return reason_code
    cleanup_suffix = "_and_cleanup_incomplete"
    if reason_code.endswith(cleanup_suffix) and reason_code[: -len(cleanup_suffix)] in _LIVE_BASE_REASON_CODES:
        return reason_code
    return "live_internal_error"


def _browser_terminal_failure_reason(value: Any) -> str:
    if type(value) is str and value in _BROWSER_TERMINAL_REASONS:
        return "browser_" + value
    return "browser_terminal_rejected"


class LiveSessionRejected(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = _normalized_live_reason(reason_code)
        super().__init__(self.reason_code)


def _reject(reason_code: str) -> NoReturn:
    raise LiveSessionRejected(reason_code)


def _run(args: Iterable[str], *, stage: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LiveSessionRejected(stage + "_unavailable") from error
    if result.returncode != 0:
        _reject(stage + "_failed")
    return result.stdout


def _resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        _reject("host_dns_failed")
    return tuple(str(row[4][0]) for row in rows)


def _registry_reference(
    *,
    repository: str,
    index_digest: str,
    config_digest: str,
) -> str:
    """Revalidate the exact pulled registry and local configuration identities."""

    if (
        type(repository) is not str
        or _REGISTRY_RE.fullmatch(repository) is None
        or type(index_digest) is not str
        or _DIGEST_RE.fullmatch(index_digest) is None
        or type(config_digest) is not str
        or _DIGEST_RE.fullmatch(config_digest) is None
    ):
        _reject("registry_identity")
    reference = repository + "@" + index_digest
    raw = _run(
        [
            "docker",
            "image",
            "inspect",
            reference,
            "--format",
            '{"config_digest":{{json .Id}},"repo_digests":{{json .RepoDigests}}}',
        ],
        stage="registry_identity",
        timeout=30,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        _reject("registry_identity_json")
    if (
        type(value) is not dict
        or set(value) != {"config_digest", "repo_digests"}
        or value["config_digest"] != config_digest
        or type(value["repo_digests"]) is not list
        or reference not in value["repo_digests"]
    ):
        _reject("registry_identity_mismatch")
    return reference


def _write_frame(stream: IO[bytes], row: Mapping[str, Any]) -> None:
    try:
        payload = (
            json.dumps(
                row,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\x00"
        )
    except (TypeError, ValueError):
        _reject("control_frame_json")
    if not 1 < len(payload) <= MAX_CONTROL_FRAME_BYTES:
        _reject("control_frame_size")
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, AttributeError):
        _reject("control_frame_write")


def _teardown_framed_resources(
    *,
    process: subprocess.Popen[bytes],
    stdin: IO[bytes] | None,
    stdout: IO[bytes] | None,
    stderr: IO[bytes] | None,
    selector: selectors.BaseSelector | None,
    stderr_thread: threading.Thread | None,
    stderr_thread_started: bool,
) -> tuple[str, ...]:
    """Best-effort teardown with fixed, content-free failure evidence."""

    failures: list[str] = []

    def attempt(reason_code: str, action: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, action()
        except Exception:
            if reason_code not in failures:
                failures.append(reason_code)
            return False, None

    if stdin is not None:
        attempt("driver_stdin_close_failed", stdin.close)
    if selector is not None:
        attempt("driver_selector_close_failed", selector.close)

    poll_ok, process_code = attempt("driver_process_poll_failed", process.poll)
    if not poll_ok or process_code is None:
        attempt("driver_process_terminate_failed", process.terminate)
        wait_ok, _ = attempt(
            "driver_process_wait_failed",
            lambda: process.wait(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS),
        )
        if not wait_ok:
            attempt("driver_process_kill_failed", process.kill)
            attempt(
                "driver_process_kill_wait_failed",
                lambda: process.wait(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS),
            )

    if stdout is not None:
        attempt("driver_stdout_close_failed", stdout.close)
    if stderr is not None:
        attempt("driver_stderr_close_failed", stderr.close)
    if stderr_thread is not None and stderr_thread_started:
        attempt(
            "driver_stderr_thread_join_failed",
            lambda: stderr_thread.join(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS),
        )
        alive_ok, is_alive = attempt(
            "driver_stderr_thread_state_failed",
            stderr_thread.is_alive,
        )
        if alive_ok and is_alive:
            failures.append("driver_stderr_thread_alive")

    final_poll_ok, final_code = attempt("driver_process_poll_failed", process.poll)
    if final_poll_ok and final_code is None:
        failures.append("driver_process_alive")
    return tuple(failures)


class _FramedProcess:
    def __init__(
        self,
        args: Iterable[str],
        *,
        stage: str,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        try:
            self.process = subprocess.Popen(
                list(args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=pass_fds,
            )
        except OSError as error:
            raise LiveSessionRejected(stage + "_unavailable") from error
        stdin: IO[bytes] | None = None
        stdout: IO[bytes] | None = None
        stderr: IO[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        stderr_thread: threading.Thread | None = None
        stderr_thread_started = False
        try:
            stdin = self.process.stdin
            stdout = self.process.stdout
            stderr = self.process.stderr
            if stdin is None or stdout is None or stderr is None:
                _reject(stage + "_pipes")
            self.stdin: IO[bytes] = stdin
            self.stdout: IO[bytes] = stdout
            self.stderr: IO[bytes] = stderr
            self._stdout_buffer = bytearray()
            self._input_finished = False
            self._stderr_digest = hashlib.sha256()
            self._stderr_bytes = 0
            self._stderr_hashed_bytes = 0
            self._stderr_overflow = False
            self._stderr_lock = threading.Lock()
            self._stderr_complete = threading.Event()
            selector = selectors.DefaultSelector()
            selector.register(self.stdout, selectors.EVENT_READ)

            def drain_stderr() -> None:
                try:
                    while True:
                        try:
                            chunk = self.stderr.read(16_384)
                        except (OSError, ValueError):
                            return
                        if not chunk:
                            return
                        with self._stderr_lock:
                            self._stderr_bytes += len(chunk)
                            remaining = max(
                                0,
                                MAX_STDERR_EVIDENCE_BYTES - self._stderr_hashed_bytes,
                            )
                            if remaining:
                                bounded = chunk[:remaining]
                                self._stderr_digest.update(bounded)
                                self._stderr_hashed_bytes += len(bounded)
                            if self._stderr_bytes > MAX_STDERR_EVIDENCE_BYTES:
                                self._stderr_overflow = True
                finally:
                    self._stderr_complete.set()

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            stderr_thread_started = True
        except Exception as error:
            failures = _teardown_framed_resources(
                process=self.process,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                selector=selector,
                stderr_thread=stderr_thread,
                stderr_thread_started=stderr_thread_started,
            )
            primary = error.reason_code if isinstance(error, LiveSessionRejected) else stage + "_setup_failed"
            if failures:
                primary = _cleanup_failure_reason(LiveSessionRejected(primary))
            raise LiveSessionRejected(primary) from error
        self._selector = selector
        self._stderr_thread = stderr_thread

    @property
    def stderr_evidence(self) -> dict[str, Any]:
        if self.process.poll() is None or not self._stderr_complete.is_set():
            _reject("control_stderr_evidence_incomplete")
        self._assert_stderr_bounded()
        with self._stderr_lock:
            return {
                "byte_count": self._stderr_bytes,
                "digest": "sha256:" + self._stderr_digest.hexdigest(),
            }

    def _assert_stderr_bounded(self) -> None:
        with self._stderr_lock:
            overflow = self._stderr_overflow
        if overflow:
            _reject("control_stderr_size")

    def _finalize_stderr(self, *, stage: str) -> None:
        self._stderr_thread.join(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS)
        if self._stderr_thread.is_alive() or not self._stderr_complete.is_set():
            _reject(stage + "_stderr_drain_timeout")
        self._assert_stderr_bounded()

    def write(self, row: Mapping[str, Any]) -> None:
        if self._input_finished:
            _reject("control_input_finished")
        _write_frame(self.stdin, row)

    def finish_input(self) -> None:
        if self._input_finished:
            _reject("control_input_finished")
        try:
            self.stdin.close()
        except OSError:
            _reject("control_input_close")
        self._input_finished = True

    def read(self, *, deadline: float, stage: str) -> bytes:
        while True:
            self._assert_stderr_bounded()
            try:
                end = self._stdout_buffer.index(0)
            except ValueError:
                end = -1
            if end >= 0:
                if end == 0:
                    _reject(stage + "_empty")
                payload = bytes(self._stdout_buffer[:end])
                del self._stdout_buffer[: end + 1]
                return payload
            if len(self._stdout_buffer) > MAX_CONTROL_FRAME_BYTES:
                _reject(stage + "_size")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reject(stage + "_timeout")
            events = self._selector.select(min(remaining, 0.5))
            if not events:
                if self.process.poll() is not None:
                    _reject(stage + "_exit")
                continue
            try:
                chunk = os.read(self.stdout.fileno(), 16_384)
            except OSError:
                _reject(stage + "_read")
            if not chunk:
                _reject(stage + "_eof")
            self._stdout_buffer.extend(chunk)

    def wait(self, *, timeout: int, stage: str) -> int:
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _reject(stage + "_timeout")
        self._finalize_stderr(stage=stage)
        if code != 0:
            _reject(stage + "_failed")
        return code

    def close(self) -> bool:
        self._input_finished = True
        failures = _teardown_framed_resources(
            process=self.process,
            stdin=self.stdin,
            stdout=self.stdout,
            stderr=self.stderr,
            selector=self._selector,
            stderr_thread=self._stderr_thread,
            stderr_thread_started=True,
        )
        return not failures


def _wait_inspect(container: str, *, timeout_seconds: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "inspect", container],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            _reject("container_inspect_unavailable")
        if result.returncode == 0:
            return result.stdout
        time.sleep(0.1)
    _reject("container_inspect_timeout")


def _cleanup_resource_identity(
    kind: str,
    identifier: str,
    *,
    session_digest: str,
    role: str,
    timeout_seconds: float = CLEANUP_INSPECT_TIMEOUT_SECONDS,
) -> tuple[str, str | None]:
    """Return absent, foreign, error, or an owned immutable Docker identity."""

    if (
        kind not in {"container", "network"}
        or type(identifier) is not str
        or not identifier
        or type(session_digest) is not str
        or _DIGEST_RE.fullmatch(session_digest) is None
        or type(timeout_seconds) is not float
        or not 0.0 < timeout_seconds <= CLEANUP_INSPECT_TIMEOUT_SECONDS
        or role
        not in {
            "managed-browser",
            "egress-broker",
            "browser-internal",
            "browser-egress",
        }
    ):
        return "error", None
    label_expression = ".Config.Labels" if kind == "container" else ".Labels"
    try:
        result = subprocess.run(
            [
                "docker",
                kind,
                "inspect",
                identifier,
                "--format",
                '{"id":{{json .Id}},"labels":{{json ' + label_expression + "}}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "error", None
    stderr = result.stderr.casefold()
    missing_markers = (
        ("no such container", "no such object") if kind == "container" else ("not found", "no such network")
    )
    if result.returncode != 0:
        if any(marker in stderr for marker in missing_markers):
            return "absent", None
        return "error", None
    try:
        if not 1 <= len(result.stdout.encode("utf-8", errors="strict")) <= MAX_CLEANUP_INSPECT_BYTES:
            return "error", None
        evidence = json.loads(result.stdout)
    except (UnicodeEncodeError, json.JSONDecodeError):
        return "error", None
    if type(evidence) is not dict or set(evidence) != {"id", "labels"}:
        return "error", None
    resource_id = evidence["id"]
    labels = evidence["labels"]
    if type(resource_id) is not str or _DOCKER_RESOURCE_ID_RE.fullmatch(resource_id) is None:
        return "error", None
    if (
        type(labels) is not dict
        or labels.get("com.algo-cli.session") != session_digest
        or labels.get("com.algo-cli.role") != role
    ):
        return "foreign", resource_id
    return "owned", resource_id


def _settled_cleanup_resource_identity(
    kind: str,
    identifier: str,
    *,
    session_digest: str,
    role: str,
) -> tuple[str, str | None]:
    """Do not accept initial absence while an attempted create may still settle."""

    deadline = time.monotonic() + CLEANUP_ABSENCE_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "absent", None
        state, resource_id = _cleanup_resource_identity(
            kind,
            identifier,
            session_digest=session_digest,
            role=role,
            timeout_seconds=min(
                CLEANUP_INSPECT_TIMEOUT_SECONDS,
                CLEANUP_ABSENCE_TIMEOUT_SECONDS,
                remaining,
            ),
        )
        if state != "absent":
            return state, resource_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "absent", None
        time.sleep(min(0.05, remaining))


def _cleanup_container(
    name: str,
    *,
    session_digest: str,
    role: str,
) -> bool:
    state, resource_id = _settled_cleanup_resource_identity(
        "container",
        name,
        session_digest=session_digest,
        role=role,
    )
    if state == "absent":
        return True
    if state != "owned" or resource_id is None:
        return False
    try:
        subprocess.run(
            ["docker", "stop", "--signal", "TERM", "--time", "3", resource_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLEANUP_MUTATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if _wait_docker_absent("container", resource_id):
        return True
    recheck, rechecked_id = _cleanup_resource_identity(
        "container",
        resource_id,
        session_digest=session_digest,
        role=role,
    )
    if recheck == "absent":
        return True
    if recheck != "owned" or rechecked_id != resource_id:
        return False
    try:
        subprocess.run(
            ["docker", "container", "rm", "--force", resource_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLEANUP_MUTATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _wait_docker_absent("container", resource_id)


def _cleanup_network(
    name: str,
    *,
    session_digest: str,
    role: str,
) -> bool:
    state, resource_id = _settled_cleanup_resource_identity(
        "network",
        name,
        session_digest=session_digest,
        role=role,
    )
    if state == "absent":
        return True
    if state != "owned" or resource_id is None:
        return False
    try:
        subprocess.run(
            ["docker", "network", "rm", resource_id],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLEANUP_MUTATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _wait_docker_absent("network", resource_id)


def _cleanup_live_resources(
    plan: BoronNetworkPlan,
    *,
    browser_process: _FramedProcess | None,
    broker_process: _FramedProcess | None,
    attempted_resources: frozenset[str],
) -> tuple[str, ...]:
    """Run every live-resource cleanup action in a fixed, isolated order."""

    if type(attempted_resources) is not frozenset or not attempted_resources <= _CLEANUP_RESOURCE_KEYS:
        return ("cleanup_scope_invalid",)
    attempted = attempted_resources
    actions: tuple[tuple[str, Callable[[], bool]], ...] = (
        (
            "browser_driver_cleanup_failed",
            lambda: True if browser_process is None else browser_process.close(),
        ),
        (
            "broker_driver_cleanup_failed",
            lambda: True if broker_process is None else broker_process.close(),
        ),
        (
            "browser_container_cleanup_failed",
            lambda: (
                True
                if "browser_container" not in attempted
                else _cleanup_container(
                    plan.browser_container,
                    session_digest=plan.session_digest,
                    role="managed-browser",
                )
            ),
        ),
        (
            "broker_container_cleanup_failed",
            lambda: (
                True
                if "broker_container" not in attempted
                else _cleanup_container(
                    plan.broker_container,
                    session_digest=plan.session_digest,
                    role="egress-broker",
                )
            ),
        ),
        (
            "egress_network_cleanup_failed",
            lambda: (
                True
                if "egress_network" not in attempted
                else _cleanup_network(
                    plan.egress_network,
                    session_digest=plan.session_digest,
                    role="browser-egress",
                )
            ),
        ),
        (
            "internal_network_cleanup_failed",
            lambda: (
                True
                if "internal_network" not in attempted
                else _cleanup_network(
                    plan.internal_network,
                    session_digest=plan.session_digest,
                    role="browser-internal",
                )
            ),
        ),
    )
    failures: list[str] = []
    for reason_code, action in actions:
        try:
            cleaned = action()
        except Exception:
            cleaned = False
        if cleaned is not True:
            failures.append(reason_code)
    return tuple(failures)


def _wait_docker_absent(kind: str, resource_id: str) -> bool:
    if kind not in {"container", "network"} or _DOCKER_RESOURCE_ID_RE.fullmatch(resource_id) is None:
        return False
    command = ["docker", kind, "inspect", resource_id]
    missing_markers = (
        ("no such container", "no such object") if kind == "container" else ("not found", "no such network")
    )
    deadline = time.monotonic() + CLEANUP_ABSENCE_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(
                    CLEANUP_INSPECT_TIMEOUT_SECONDS,
                    CLEANUP_ABSENCE_TIMEOUT_SECONDS,
                    remaining,
                ),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        stderr = result.stderr.casefold()
        if result.returncode != 0 and any(marker in stderr for marker in missing_markers):
            return True
        if result.returncode != 0:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _cleanup_failure_reason(primary_error: BaseException | None) -> str:
    if primary_error is None:
        return "cleanup_incomplete"
    reason = getattr(primary_error, "reason_code", None)
    if (
        isinstance(primary_error, LiveSessionRejected)
        and type(reason) is str
        and _PRIMARY_REASON_CODE_RE.fullmatch(reason)
        and reason in _LIVE_BASE_REASON_CODES
    ):
        return reason + "_and_cleanup_incomplete"
    return "live_failure_and_cleanup_incomplete"


def _reported_failure_reason(error: BaseException) -> str:
    if isinstance(error, LiveSessionRejected):
        return error.reason_code
    fixed = (
        (BuildRejected, "browser_build_rejected"),
        (BoronIsolationRejected, "browser_isolation_rejected"),
        (BoronEntryRejected, "browser_entry_rejected"),
        (BoronPipeRejected, "browser_pipe_rejected"),
        (XenonBrokerRejected, "broker_rejected"),
        (XenonEntryRejected, "broker_entry_rejected"),
    )
    for error_type, reason_code in fixed:
        if isinstance(error, error_type):
            return reason_code
    return "live_internal_error"


def _assert_native_amd64_docker() -> str:
    observed = _run(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"],
        stage="docker_platform",
        timeout=30,
    ).strip()
    if observed not in {"linux/amd64", "linux/x86_64"}:
        _reject("live_platform_emulation_forbidden")
    return "linux/amd64"


def _validated_build_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the exact build evidence before it crosses into a live run."""

    if type(value) is not dict or set(value) != _BUILD_EVIDENCE_KEYS:
        _reject("browser_build_evidence_shape")
    evidence = dict(value)
    string_fields = (
        "platform",
        "browser_tag",
        "browser_repository",
        "broker_tag",
        "broker_repository",
        "browser_version",
        "browser_security_source",
        "native_browser_freshness_reason",
        "cryptography_version",
        "image_provenance",
    )
    if (
        type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 2
        or any(type(evidence[field]) is not str for field in string_fields)
        or evidence["platform"] != PLATFORM
        or _REGISTRY_TAG_RE.fullmatch(evidence["browser_tag"]) is None
        or _REGISTRY_TAG_RE.fullmatch(evidence["broker_tag"]) is None
        or _REGISTRY_RE.fullmatch(evidence["browser_repository"]) is None
        or _REGISTRY_RE.fullmatch(evidence["broker_repository"]) is None
        or evidence["browser_tag"].rsplit(":", 1)[0] != evidence["browser_repository"]
        or evidence["broker_tag"].rsplit(":", 1)[0] != evidence["broker_repository"]
        or evidence["browser_repository"] != "ghcr.io/seabass-up/algo-cli-boron-browser"
        or evidence["broker_repository"] != "ghcr.io/seabass-up/algo-cli-xenon-broker"
        or evidence["browser_version"] != CHROME_VERSION
        or evidence["browser_security_max_update_lag_ms"] != BORON_MAX_SECURITY_LAG_MS
        or type(evidence["browser_security_max_update_lag_ms"]) is not int
        or evidence["browser_security_source"] != BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY.value
        or evidence["native_browser_built"] is not False
        or evidence["native_browser_fresh"] is not False
        or evidence["native_browser_freshness_reason"] != "upstream_patch_equivalence_unverified"
        or evidence["cryptography_version"] != CRYPTOGRAPHY_VERSION
        or evidence["image_provenance"] != "ghcr_buildkit_max_sbom"
        or evidence["non_root_defaults"] is not True
    ):
        _reject("browser_build_evidence_identity")
    digest_fields = (
        "qualification_source_digest",
        "browser_index_digest",
        "browser_platform_manifest_digest",
        "browser_config_digest",
        "browser_build_metadata_digest",
        "browser_provenance_digest",
        "browser_sbom_digest",
        "browser_code_digest",
        "browser_security_source_digest",
        "broker_index_digest",
        "broker_platform_manifest_digest",
        "broker_config_digest",
        "broker_build_metadata_digest",
        "broker_provenance_digest",
        "broker_sbom_digest",
        "broker_code_digest",
    )
    if any(
        type(evidence[field]) is not str or _DIGEST_RE.fullmatch(evidence[field]) is None for field in digest_fields
    ):
        _reject("browser_build_evidence_digest")
    if (
        type(evidence["browser_security_latest_version"]) is not str
        or re.fullmatch(
            r"[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}",
            evidence["browser_security_latest_version"],
        )
        is None
    ):
        _reject("browser_build_evidence_version")
    for field in (
        "browser_security_update_lag_ms",
        "browser_security_latest_release_at_ms",
        "browser_security_evidence_observed_at_ms",
    ):
        if type(evidence[field]) is not int:
            _reject("browser_build_evidence_time")
    if (
        not 0 <= evidence["browser_security_update_lag_ms"] <= BORON_MAX_SECURITY_LAG_MS
        or not 1 <= evidence["browser_security_latest_release_at_ms"] <= (1 << 53) - 1
        or not evidence["browser_security_latest_release_at_ms"]
        <= evidence["browser_security_evidence_observed_at_ms"]
        <= (1 << 53) - 1
    ):
        _reject("browser_build_evidence_time")
    return evidence


def _assert_build_image_binding(
    build: Mapping[str, Any],
    *,
    browser_image: BoronImagePin,
    broker_image: BoronBrokerImagePin,
) -> None:
    """Reject a tag replacement between the attested build and live launch."""

    if (
        type(build) is not dict
        or type(browser_image) is not BoronImagePin
        or type(broker_image) is not BoronBrokerImagePin
    ):
        _reject("live_build_image_binding")
    if browser_image.digest != build.get("browser_index_digest"):
        _reject("live_browser_image_changed")
    if broker_image.digest != build.get("broker_index_digest"):
        _reject("live_broker_image_changed")
    if broker_image.binary_digest != build.get("broker_code_digest"):
        _reject("live_broker_code_changed")


def _sealed_seccomp_profile(payload: bytes) -> tuple[int, Path]:
    """Return one sealed Linux memfd path containing the exact supplied bytes."""

    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_SECCOMP_PROFILE_BYTES:
        _reject("live_seccomp_profile")
    descriptor: int | None = None
    try:
        import fcntl

        memfd_create = getattr(os, "memfd_create", None)
        close_on_exec = getattr(os, "MFD_CLOEXEC", None)
        allow_sealing = getattr(os, "MFD_ALLOW_SEALING", None)
        add_seals = getattr(fcntl, "F_ADD_SEALS", None)
        get_seals = getattr(fcntl, "F_GET_SEALS", None)
        seal_write = getattr(fcntl, "F_SEAL_WRITE", None)
        seal_shrink = getattr(fcntl, "F_SEAL_SHRINK", None)
        seal_grow = getattr(fcntl, "F_SEAL_GROW", None)
        seal_seal = getattr(fcntl, "F_SEAL_SEAL", None)
        if (
            not callable(memfd_create)
            or type(close_on_exec) is not int
            or type(allow_sealing) is not int
            or type(add_seals) is not int
            or type(get_seals) is not int
            or type(seal_write) is not int
            or type(seal_shrink) is not int
            or type(seal_grow) is not int
            or type(seal_seal) is not int
        ):
            _reject("live_seccomp_unavailable")
        descriptor = memfd_create(
            "algo-cli-boron-seccomp",
            close_on_exec | allow_sealing,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count < 1:
                _reject("live_seccomp_unavailable")
            written += count
        os.fsync(descriptor)
        seals = seal_write | seal_shrink | seal_grow | seal_seal
        fcntl.fcntl(descriptor, add_seals, seals)
        if fcntl.fcntl(descriptor, get_seals) != seals:
            _reject("live_seccomp_unavailable")
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = bytearray()
        while len(observed) < len(payload):
            chunk = os.read(descriptor, min(16_384, len(payload) - len(observed)))
            if not chunk:
                _reject("live_seccomp_unavailable")
            observed.extend(chunk)
        if os.read(descriptor, 1) or bytes(observed) != payload:
            _reject("live_seccomp_unavailable")
        path = Path(f"/proc/self/fd/{descriptor}")
        if not path.is_file():
            _reject("live_seccomp_unavailable")
        result = (descriptor, path)
        descriptor = None
        return result
    except (AttributeError, ImportError, OSError, ValueError):
        _reject("live_seccomp_unavailable")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _local_seccomp_payload() -> bytes:
    """Read the local-only fallback without following a final-path symlink."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            SECCOMP,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if not 1 <= info.st_size <= MAX_SECCOMP_PROFILE_BYTES:
            _reject("live_seccomp_profile")
        payload = bytearray()
        while len(payload) < info.st_size:
            chunk = os.read(descriptor, min(16_384, info.st_size - len(payload)))
            if not chunk:
                _reject("live_seccomp_profile")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            _reject("live_seccomp_profile")
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or after.st_mode != info.st_mode
            or after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or after.st_nlink != 1
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
            or after.st_ctime_ns != info.st_ctime_ns
        ):
            _reject("live_seccomp_profile")
        return bytes(payload)
    except OSError:
        _reject("live_seccomp_unavailable")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def run_live_session(
    *,
    build_evidence: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
    seccomp_profile: bytes | None = None,
) -> dict[str, Any]:
    hosted_environment = dict(os.environ) if environment is None else environment
    if build_evidence is None:
        _reject("live_build_evidence_required")
    build = _validated_build_evidence(build_evidence)
    expected_browser_tag, expected_broker_tag = hosted_registry_tags(hosted_environment)
    if build["browser_tag"] != expected_browser_tag or build["broker_tag"] != expected_broker_tag:
        _reject("live_registry_run_binding")
    if seccomp_profile is None and hosted_environment.get("GITHUB_ACTIONS") == "true":
        _reject("live_seccomp_profile_required")
    live_platform = _assert_native_amd64_docker()
    browser_reference = _registry_reference(
        repository=build["browser_repository"],
        index_digest=build["browser_index_digest"],
        config_digest=build["browser_config_digest"],
    )
    broker_reference = _registry_reference(
        repository=build["broker_repository"],
        index_digest=build["broker_index_digest"],
        config_digest=build["broker_config_digest"],
    )
    now_ms = int(time.time() * 1000)
    try:
        release_evidence = BoronBrowserReleaseEvidence(
            source=BoronReleaseEvidenceSource(build["browser_security_source"]),
            browser_family=BoronBrowserFamily.CHROME_STABLE,
            browser_version=build["browser_security_latest_version"],
            platform=build["platform"],
            security_release_at_ms=build["browser_security_latest_release_at_ms"],
            observed_at_ms=build["browser_security_evidence_observed_at_ms"],
            source_digest=build["browser_security_source_digest"],
        )
    except (KeyError, TypeError, ValueError, BoronIsolationRejected) as error:
        raise LiveSessionRejected("browser_security_evidence_shape") from error
    suffix = secrets.token_hex(4)
    session_digest = "sha256:" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    third_octet = 32 + secrets.randbelow(160)
    plan = BoronNetworkPlan(
        session_digest=session_digest,
        internal_network="boron-private-" + suffix,
        egress_network="xenon-egress-" + suffix,
        browser_container="boron-browser-" + suffix,
        broker_container="xenon-broker-" + suffix,
        internal_subnet=f"172.30.{third_octet}.0/24",
        internal_gateway=f"172.30.{third_octet}.1",
        browser_internal_ip=f"172.30.{third_octet}.2",
        broker_internal_ip=f"172.30.{third_octet}.3",
    )
    browser_image = BoronImagePin(
        browser_reference,
        BoronImagePurpose.PUBLIC_MANAGED,
        BoronBrowserFamily.CHROME_STABLE,
        CHROME_VERSION,
        PLATFORM,
        CHROME_RELEASE_AT_MS,
    )
    broker_image = BoronBrokerImagePin(
        broker_reference,
        PLATFORM,
        str(build["broker_code_digest"]),
    )
    _assert_build_image_binding(
        build,
        browser_image=browser_image,
        broker_image=broker_image,
    )
    if seccomp_profile is None:
        seccomp_profile = _local_seccomp_payload()
    seccomp_descriptor, seccomp_path = _sealed_seccomp_profile(seccomp_profile)
    try:
        browser_launch = BoronBrowserLaunch(browser_image, plan, seccomp_path)
        broker_launch = BoronBrokerLaunch(broker_image, plan, seccomp_path)
        browser_image.assert_fresh(
            now_ms=now_ms,
            release_evidence=release_evidence,
        )
    except BaseException:
        try:
            os.close(seccomp_descriptor)
        except OSError:
            pass
        raise

    browser_process: _FramedProcess | None = None
    broker_attach: _FramedProcess | None = None
    attempted_resources: set[str] = set()
    try:
        attempted_resources.add("internal_network")
        _run(browser_launch.create_internal_network_argv(), stage="internal_network_create")
        attempted_resources.add("egress_network")
        _run(broker_launch.create_egress_network_argv(), stage="egress_network_create")
        attempted_resources.add("broker_container")
        broker_attach = _FramedProcess(
            broker_launch.broker_foreground_argv(),
            stage="broker_start",
            pass_fds=(seccomp_descriptor,),
        )
        _wait_inspect(plan.broker_container)
        _run(broker_launch.connect_egress_network_argv(), stage="broker_egress_connect")
        issued_at_ms = int(time.time() * 1000)
        authority_key = secrets.token_bytes(32)
        fencing_token = secrets.randbelow((1 << 52) - 1) + 1
        permit = issue_xenon_broker_permit(
            authority_key=authority_key,
            raw_url=TARGET_URL,
            resolver=_resolver,
            issued_at_ms=issued_at_ms,
            expires_at_ms=issued_at_ms + 120_000,
            fencing_token=fencing_token,
            maximum_connections=16,
            maximum_active_connections=8,
            maximum_response_bytes=4 * 1024 * 1024,
            maximum_total_bytes=16 * 1024 * 1024,
            maximum_redirects=2,
        )
        broker_attach.write(
            {
                "schema_version": XENON_BROKER_SCHEMA_VERSION,
                "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
                "type": "xenon.start",
                "authority_key_base64url": base64.urlsafe_b64encode(authority_key).decode("ascii").rstrip("="),
                "permit": permit.to_dict(),
                "expected_fencing_token": fencing_token,
            }
        )
        broker_attach.finish_input()
        ready = read_xenon_entry_frame(
            BytesIO(broker_attach.read(deadline=time.monotonic() + 30, stage="broker_ready") + b"\x00")
        )
        if ready.get("type") != "xenon.ready" or ready.get("permit_id") != permit.permit_id:
            _reject("broker_ready_identity")

        attempted_resources.add("browser_container")
        browser_process = _FramedProcess(
            browser_launch.browser_argv(),
            stage="browser_start",
            pass_fds=(seccomp_descriptor,),
        )
        browser_inspect = _wait_inspect(plan.browser_container)
        broker_inspect = _wait_inspect(plan.broker_container)
        internal_inspect = _run(
            ["docker", "network", "inspect", plan.internal_network],
            stage="internal_network_inspect",
        )
        egress_inspect = _run(
            ["docker", "network", "inspect", plan.egress_network],
            stage="egress_network_inspect",
        )
        topology = verify_docker_topology(
            plan,
            browser_image,
            broker_image,
            browser_runtime_image_id=build["browser_config_digest"],
            broker_runtime_image_id=build["broker_config_digest"],
            internal_network_json=internal_inspect,
            egress_network_json=egress_inspect,
            browser_inspect_json=browser_inspect,
            broker_inspect_json=broker_inspect,
        )

        browser_row = {
            "schema_version": BORON_ENTRY_SCHEMA_VERSION,
            "protocol_version": BORON_ENTRY_PROTOCOL_VERSION,
            "type": "boron.start",
            "session_id": permit.session_id,
            "canonical_url": permit.canonical_url,
            "expected_browser_version": CHROME_VERSION,
            "proxy_host": plan.broker_alias,
            "proxy_port": plan.broker_port,
            "maximum_duration_ms": 60_000,
            "ca_pem_base64url": ready.get("ca_pem_base64url"),
            "ca_pem_digest": ready.get("ca_pem_digest"),
            "ca_certificate_digest": ready.get("ca_certificate_digest"),
        }
        BoronStartConfig.from_dict(browser_row)
        browser_process.write(browser_row)
        browser_process.finish_input()
        browser_result = decode_boron_pipe_message(
            browser_process.read(deadline=time.monotonic() + 90, stage="browser_result")
        )
        if browser_result.get("type") == "boron.error":
            reason = browser_result.get("reason_code")
            if type(reason) is not str or not reason:
                _reject("browser_error_shape")
            _reject("browser_entry_rejected")
        if browser_result.get("type") != "boron.result":
            _reject("browser_result_type")
        if browser_result.get("state") != "verified":
            _reject(_browser_terminal_failure_reason(browser_result.get("reason_code")))
        if browser_result.get("ca_certificate_digest") != ready.get("ca_certificate_digest"):
            _reject("browser_ca_identity")
        browser_process.wait(timeout=15, stage="browser_exit")

        if not _cleanup_container(
            plan.broker_container,
            session_digest=plan.session_digest,
            role="egress-broker",
        ):
            _reject("broker_stop_failed")
        broker_result = read_xenon_entry_frame(
            BytesIO(broker_attach.read(deadline=time.monotonic() + 20, stage="broker_result") + b"\x00")
        )
        broker_attach.wait(timeout=15, stage="broker_attach_exit")
        if (
            broker_result.get("type") != "xenon.result"
            or broker_result.get("disposition") != "verified"
            or type(broker_result.get("connection_count")) is not int
            or broker_result["connection_count"] < 1
            or type(broker_result.get("request_count")) is not int
            or broker_result["request_count"] < 1
            or type(broker_result.get("bytes_to_browser")) is not int
            or broker_result["bytes_to_browser"] < 1
            or broker_result.get("ca_certificate_digest") != ready.get("ca_certificate_digest")
        ):
            _reject("broker_result_invariant")
        return {
            "schema_version": 2,
            "platform": live_platform,
            "qualification_source_digest": build["qualification_source_digest"],
            "browser_index_digest": browser_image.digest,
            "browser_platform_manifest_digest": build["browser_platform_manifest_digest"],
            "browser_config_digest": build["browser_config_digest"],
            "browser_build_metadata_digest": build["browser_build_metadata_digest"],
            "browser_provenance_digest": build["browser_provenance_digest"],
            "browser_sbom_digest": build["browser_sbom_digest"],
            "broker_index_digest": broker_image.digest,
            "broker_platform_manifest_digest": build["broker_platform_manifest_digest"],
            "broker_config_digest": build["broker_config_digest"],
            "broker_build_metadata_digest": build["broker_build_metadata_digest"],
            "broker_provenance_digest": build["broker_provenance_digest"],
            "broker_sbom_digest": build["broker_sbom_digest"],
            "broker_code_digest": broker_image.binary_digest,
            "topology_evidence_digest": topology.evidence_digest,
            "internal_participant_count": topology.participant_count,
            "browser_state": browser_result["state"],
            "browser_major": browser_result["browser_major"],
            "browser_security_update_lag_ms": browser_image.security_update_lag_ms(
                now_ms=now_ms,
                release_evidence=release_evidence,
            ),
            "browser_security_source_digest": release_evidence.source_digest,
            "browser_command_count": browser_result["command_count"],
            "browser_event_count": browser_result["event_count"],
            "broker_disposition": broker_result["disposition"],
            "broker_connection_count": broker_result["connection_count"],
            "broker_request_count": broker_result["request_count"],
            "broker_redirect_count": broker_result["redirect_count"],
            "broker_bytes_to_browser": broker_result["bytes_to_browser"],
            "target_decision_digest": broker_result["target_decision_digest"],
            "ca_certificate_digest": broker_result["ca_certificate_digest"],
            "browser_stderr": browser_process.stderr_evidence,
            "broker_stderr": broker_attach.stderr_evidence,
        }
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_failures: tuple[str, ...] = ()
        seccomp_close_failed = False
        try:
            cleanup_failures = _cleanup_live_resources(
                plan,
                browser_process=browser_process,
                broker_process=broker_attach,
                attempted_resources=frozenset(attempted_resources),
            )
        finally:
            try:
                os.close(seccomp_descriptor)
            except OSError:
                seccomp_close_failed = True
        if cleanup_failures:
            raise LiveSessionRejected(_cleanup_failure_reason(primary_error)) from primary_error
        if seccomp_close_failed and primary_error is None:
            raise LiveSessionRejected("live_seccomp_close_failed") from None


def main() -> int:
    try:
        evidence = run_live_session()
    except (
        LiveSessionRejected,
        BuildRejected,
        BoronIsolationRejected,
        BoronEntryRejected,
        BoronPipeRejected,
        XenonBrokerRejected,
        XenonEntryRejected,
    ) as error:
        reason_code = _reported_failure_reason(error)
        print(json.dumps({"status": "failed", "reason_code": reason_code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "reason_code": "live_internal_error"}, sort_keys=True))
        return 1
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("ascii")
    print(
        json.dumps(
            {
                "status": "passed",
                "evidence": evidence,
                "evidence_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                "limitation": LIVE_EVIDENCE_LIMITATION,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
