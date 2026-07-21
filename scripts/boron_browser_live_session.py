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
import subprocess
import threading
import time
from typing import Any, IO, Iterable, Mapping, NoReturn

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
    BROWSER_TAG,
    BROKER_TAG,
    CHROME_RELEASE_AT_MS,
    CHROME_VERSION,
    PLATFORM,
    BuildRejected,
    build_images,
)


ROOT = Path(__file__).resolve().parents[1]
SECCOMP = (ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json").resolve()
TARGET_URL = "https://example.com/"
MAX_CONTROL_FRAME_BYTES = 131_072
MAX_STDERR_EVIDENCE_BYTES = 1_048_576
DRIVER_SHUTDOWN_TIMEOUT_SECONDS = 3.0
LIVE_EVIDENCE_LIMITATION = (
    "One live public GET on native amd64 Linux Docker; not product readiness "
    "or broad-site compatibility."
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUILD_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "platform",
        "browser_tag",
        "browser_image_id",
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
        "broker_image_id",
        "broker_code_digest",
        "cryptography_version",
        "non_root_defaults",
    }
)


class LiveSessionRejected(RuntimeError):
    pass


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


def _repo_digest(tag: str) -> str:
    raw = _run(
        ["docker", "image", "inspect", tag, "--format", "{{json .RepoDigests}}"],
        stage="repo_digest",
        timeout=30,
    )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        _reject("repo_digest_json")
    repository = tag.rsplit(":", 1)[0]
    if type(values) is not list:
        _reject("repo_digest_shape")
    matching = [
        value
        for value in values
        if type(value) is str and value.startswith(repository + "@sha256:")
    ]
    if len(matching) != 1:
        _reject("repo_digest_shape")
    return matching[0]


def _write_frame(stream: IO[bytes], row: Mapping[str, Any]) -> None:
    try:
        payload = json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\x00"
    except (TypeError, ValueError):
        _reject("control_frame_json")
    if not 1 < len(payload) <= MAX_CONTROL_FRAME_BYTES:
        _reject("control_frame_size")
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, AttributeError):
        _reject("control_frame_write")


class _FramedProcess:
    def __init__(self, args: Iterable[str], *, stage: str) -> None:
        try:
            self.process = subprocess.Popen(
                list(args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as error:
            raise LiveSessionRejected(stage + "_unavailable") from error
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
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.stdout, selectors.EVENT_READ)

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

        self._stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        self._stderr_thread.start()

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
        if not self._input_finished:
            try:
                self.stdin.close()
            except OSError:
                pass
            self._input_finished = True
        self._selector.close()
        if self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                    self.process.wait(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    return False
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                return False
        self._stderr_thread.join(timeout=DRIVER_SHUTDOWN_TIMEOUT_SECONDS)
        return self.process.poll() is not None and not self._stderr_thread.is_alive()


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


def _cleanup_container(name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "stop", "--signal", "TERM", "--time", "3", name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 or "No such container" in result.stderr


def _cleanup_network(name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "network", "rm", name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 or "not found" in result.stderr.casefold()


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
        "broker_tag",
        "browser_version",
        "browser_security_source",
        "native_browser_freshness_reason",
        "cryptography_version",
    )
    if (
        type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 1
        or any(type(evidence[field]) is not str for field in string_fields)
        or evidence["platform"] != PLATFORM
        or evidence["browser_tag"] != BROWSER_TAG
        or evidence["broker_tag"] != BROKER_TAG
        or evidence["browser_version"] != CHROME_VERSION
        or evidence["browser_security_max_update_lag_ms"]
        != BORON_MAX_SECURITY_LAG_MS
        or type(evidence["browser_security_max_update_lag_ms"]) is not int
        or evidence["browser_security_source"]
        != BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY.value
        or evidence["native_browser_built"] is not False
        or evidence["native_browser_fresh"] is not False
        or evidence["native_browser_freshness_reason"]
        != "upstream_patch_equivalence_unverified"
        or evidence["cryptography_version"] != "49.0.0"
        or evidence["non_root_defaults"] is not True
    ):
        _reject("browser_build_evidence_identity")
    digest_fields = (
        "browser_image_id",
        "browser_code_digest",
        "browser_security_source_digest",
        "broker_image_id",
        "broker_code_digest",
    )
    if any(
        type(evidence[field]) is not str
        or _DIGEST_RE.fullmatch(evidence[field]) is None
        for field in digest_fields
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
        not 0
        <= evidence["browser_security_update_lag_ms"]
        <= BORON_MAX_SECURITY_LAG_MS
        or not 1
        <= evidence["browser_security_latest_release_at_ms"]
        <= (1 << 53) - 1
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
    if browser_image.digest != build.get("browser_image_id"):
        _reject("live_browser_image_changed")
    if broker_image.digest != build.get("broker_image_id"):
        _reject("live_broker_image_changed")
    if broker_image.binary_digest != build.get("broker_code_digest"):
        _reject("live_broker_binary_changed")


def run_live_session(
    *, build_evidence: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    live_platform = _assert_native_amd64_docker()
    build = _validated_build_evidence(
        build_images() if build_evidence is None else build_evidence
    )
    browser_reference = _repo_digest(BROWSER_TAG)
    broker_reference = _repo_digest(BROKER_TAG)
    now_ms = int(time.time() * 1000)
    try:
        release_evidence = BoronBrowserReleaseEvidence(
            source=BoronReleaseEvidenceSource(build["browser_security_source"]),
            browser_family=BoronBrowserFamily.CHROME_STABLE,
            browser_version=build["browser_security_latest_version"],
            platform=build["platform"],
            security_release_at_ms=build[
                "browser_security_latest_release_at_ms"
            ],
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
    browser_launch = BoronBrowserLaunch(browser_image, plan, SECCOMP)
    broker_launch = BoronBrokerLaunch(broker_image, plan, SECCOMP)
    browser_image.assert_fresh(
        now_ms=now_ms,
        release_evidence=release_evidence,
    )

    cleanup = {
        "browser": False,
        "broker": False,
        "browser_driver": False,
        "broker_driver": False,
        "internal_network": False,
        "egress_network": False,
    }
    browser_process: _FramedProcess | None = None
    broker_attach: _FramedProcess | None = None
    internal_created = False
    egress_created = False
    broker_started = False
    browser_started = False
    completed = False
    try:
        _run(browser_launch.create_internal_network_argv(), stage="internal_network_create")
        internal_created = True
        _run(broker_launch.create_egress_network_argv(), stage="egress_network_create")
        egress_created = True
        broker_attach = _FramedProcess(
            broker_launch.broker_foreground_argv(),
            stage="broker_start",
        )
        broker_started = True
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
                "authority_key_base64url": base64.urlsafe_b64encode(authority_key)
                .decode("ascii")
                .rstrip("="),
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

        browser_process = _FramedProcess(browser_launch.browser_argv(), stage="browser_start")
        browser_started = True
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
            _reject("browser_" + reason)
        if browser_result.get("type") != "boron.result":
            _reject("browser_result_type")
        if browser_result.get("state") != "verified":
            reason = browser_result.get("reason_code")
            _reject("browser_" + reason if type(reason) is str else "browser_not_verified")
        if browser_result.get("ca_certificate_digest") != ready.get(
            "ca_certificate_digest"
        ):
            _reject("browser_ca_identity")
        browser_process.wait(timeout=15, stage="browser_exit")

        if not _cleanup_container(plan.broker_container):
            _reject("broker_stop_failed")
        cleanup["broker"] = True
        broker_started = False
        broker_result = read_xenon_entry_frame(
            BytesIO(
                broker_attach.read(deadline=time.monotonic() + 20, stage="broker_result")
                + b"\x00"
            )
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
            or broker_result.get("ca_certificate_digest")
            != ready.get("ca_certificate_digest")
        ):
            _reject("broker_result_invariant")
        completed = True
        cleanup["browser"] = True
        browser_started = False
        return {
            "schema_version": 1,
            "platform": live_platform,
            "browser_image_digest": browser_image.digest,
            "broker_image_digest": broker_image.digest,
            "broker_binary_digest": broker_image.binary_digest,
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
        if browser_started:
            cleanup["browser"] = _cleanup_container(plan.browser_container)
        if broker_started:
            cleanup["broker"] = _cleanup_container(plan.broker_container)
        if browser_process is not None:
            cleanup["browser_driver"] = browser_process.close()
        if broker_attach is not None:
            cleanup["broker_driver"] = broker_attach.close()
        if egress_created:
            cleanup["egress_network"] = _cleanup_network(plan.egress_network)
        if internal_created:
            cleanup["internal_network"] = _cleanup_network(plan.internal_network)
        if completed and not all(cleanup.values()):
            _reject("cleanup_incomplete")


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
        reason_code = getattr(error, "reason_code", str(error))
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
