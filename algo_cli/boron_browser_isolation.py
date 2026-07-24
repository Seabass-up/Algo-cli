"""Docker-backed isolation contract for the disabled managed-browser route.

Chrome flags and an ephemeral profile are defense in depth, not the boundary.
This module requires an internal Docker network, a separate egress broker, a
digest-pinned image, no host mounts or published ports, and a non-root,
read-only, capability-free browser container.  A trusted-fixture image is a
different type and can never be used for public browsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, NoReturn, Sequence


BORON_PROTOCOL_VERSION = 1
BORON_MAX_BROWSER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
BORON_MAX_BROWSER_PIDS = 256
BORON_MAX_BROKER_MEMORY_BYTES = 512 * 1024 * 1024
BORON_MAX_BROKER_PIDS = 64
BORON_MAX_SECURITY_LAG_MS = 72 * 60 * 60 * 1000
BORON_MAX_RELEASE_EVIDENCE_AGE_MS = 5 * 60 * 1000

_IMAGE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._/-]{0,254})@sha256:(?P<digest>[0-9a-f]{64})$"
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VERSION_RE = re.compile(r"^[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BoronIsolationRejected(ValueError):
    """A browser launch or Docker evidence object failed closed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class BoronImagePurpose(str, Enum):
    PUBLIC_MANAGED = "public_managed"
    TRUSTED_FIXTURE = "trusted_fixture"


class BoronBrowserFamily(str, Enum):
    CHROME_STABLE = "chrome_stable"
    CHROMIUM_STABLE = "chromium_stable"
    CHROME_FOR_TESTING = "chrome_for_testing"


class BoronReleaseEvidenceSource(str, Enum):
    GOOGLE_VERSION_HISTORY = "google_version_history"


class BoronReadinessState(str, Enum):
    READY = "ready"
    DOCKER_NOT_INSTALLED = "docker_not_installed"
    DOCKER_DAEMON_UNAVAILABLE = "docker_daemon_unavailable"
    DOCKER_SECURITY_UNAVAILABLE = "docker_security_unavailable"
    IMAGE_NOT_INSTALLED = "image_not_installed"
    IMAGE_IDENTITY_MISMATCH = "image_identity_mismatch"


def _reject(reason_code: str) -> NoReturn:
    raise BoronIsolationRejected(reason_code)


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _reject(label)
    return value


def _name(value: Any, label: str) -> str:
    if type(value) is not str or not _NAME_RE.fullmatch(value):
        _reject(label)
    return value


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    if not _VERSION_RE.fullmatch(value):
        _reject("browser_version")
    major, minor, build, patch = value.split(".")
    return int(major), int(minor), int(build), int(patch)


@dataclass(frozen=True, slots=True)
class BoronBrowserReleaseEvidence:
    """A recent observation of the authoritative current browser release."""

    source: BoronReleaseEvidenceSource
    browser_family: BoronBrowserFamily
    browser_version: str
    platform: str
    security_release_at_ms: int
    observed_at_ms: int
    source_digest: str

    def __post_init__(self) -> None:
        if type(self.source) is not BoronReleaseEvidenceSource:
            _reject("browser_security_evidence_source")
        if type(self.browser_family) is not BoronBrowserFamily:
            _reject("browser_security_evidence_family")
        if self.source is BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY and (
            self.browser_family is not BoronBrowserFamily.CHROME_STABLE
            or self.platform != "linux/amd64"
        ):
            _reject("browser_security_evidence_scope")
        if type(self.browser_version) is not str or not _VERSION_RE.fullmatch(
            self.browser_version
        ):
            _reject("browser_security_evidence_version")
        _integer(
            self.security_release_at_ms,
            "browser_security_evidence_release_at_ms",
            1,
            (1 << 53) - 1,
        )
        _integer(
            self.observed_at_ms,
            "browser_security_evidence_observed_at_ms",
            1,
            (1 << 53) - 1,
        )
        if self.observed_at_ms < self.security_release_at_ms:
            _reject("browser_security_evidence_clock_regression")
        if type(self.source_digest) is not str or not _DIGEST_RE.fullmatch(
            self.source_digest
        ):
            _reject("browser_security_evidence_digest")

    def assert_current(self, *, now_ms: int) -> None:
        now = _integer(now_ms, "now_ms", 1, (1 << 53) - 1)
        if now < self.observed_at_ms:
            _reject("browser_security_evidence_clock_regression")
        if now - self.observed_at_ms > BORON_MAX_RELEASE_EVIDENCE_AGE_MS:
            _reject("browser_security_evidence_stale")


@dataclass(frozen=True, slots=True)
class BoronImagePin:
    reference: str
    purpose: BoronImagePurpose
    browser_family: BoronBrowserFamily
    browser_version: str
    platform: str
    security_release_at_ms: int

    def __post_init__(self) -> None:
        if type(self.reference) is not str or not _IMAGE_RE.fullmatch(self.reference):
            _reject("image_digest_required")
        if type(self.purpose) is not BoronImagePurpose:
            _reject("image_purpose")
        if type(self.browser_family) is not BoronBrowserFamily:
            _reject("browser_family")
        if type(self.browser_version) is not str or not _VERSION_RE.fullmatch(
            self.browser_version
        ):
            _reject("browser_version")
        if self.platform not in {"linux/arm64", "linux/amd64"}:
            _reject("image_platform")
        _integer(
            self.security_release_at_ms,
            "security_release_at_ms",
            1,
            (1 << 53) - 1,
        )
        repository = _IMAGE_RE.fullmatch(self.reference).group("repository")  # type: ignore[union-attr]
        fixture_markers = ("playwright", "chrome-for-testing", "chrome_for_testing")
        if self.purpose is BoronImagePurpose.PUBLIC_MANAGED:
            if self.browser_family is BoronBrowserFamily.CHROME_FOR_TESTING:
                _reject("testing_browser_public_route")
            if any(marker in repository for marker in fixture_markers):
                _reject("testing_image_public_route")
        elif self.browser_family is not BoronBrowserFamily.CHROME_FOR_TESTING:
            _reject("fixture_browser_family")

    @property
    def digest(self) -> str:
        match = _IMAGE_RE.fullmatch(self.reference)
        if match is None:  # pragma: no cover - constructor invariant
            _reject("image_digest_required")
        return "sha256:" + match.group("digest")

    def security_update_lag_ms(
        self,
        *,
        now_ms: int,
        release_evidence: BoronBrowserReleaseEvidence | None = None,
    ) -> int:
        now = _integer(now_ms, "now_ms", 1, (1 << 53) - 1)
        if now < self.security_release_at_ms:
            _reject("security_clock_regression")
        if self.purpose is BoronImagePurpose.TRUSTED_FIXTURE:
            return 0
        if type(release_evidence) is not BoronBrowserReleaseEvidence:
            _reject("browser_security_evidence_required")
        release_evidence.assert_current(now_ms=now)
        if (
            release_evidence.browser_family is not self.browser_family
            or release_evidence.platform != self.platform
        ):
            _reject("browser_security_evidence_mismatch")
        pinned_version = _version_tuple(self.browser_version)
        latest_version = _version_tuple(release_evidence.browser_version)
        if pinned_version > latest_version:
            _reject("browser_security_feed_regression")
        if pinned_version == latest_version:
            if self.security_release_at_ms != release_evidence.security_release_at_ms:
                _reject("browser_security_release_timestamp_mismatch")
            return 0
        lag_ms = now - release_evidence.security_release_at_ms
        if lag_ms > BORON_MAX_SECURITY_LAG_MS:
            _reject("browser_security_update_stale")
        return lag_ms

    def assert_fresh(
        self,
        *,
        now_ms: int,
        release_evidence: BoronBrowserReleaseEvidence | None = None,
    ) -> None:
        self.security_update_lag_ms(
            now_ms=now_ms,
            release_evidence=release_evidence,
        )


@dataclass(frozen=True, slots=True)
class BoronBrokerImagePin:
    reference: str
    platform: str
    binary_digest: str
    protocol_version: int = BORON_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if type(self.reference) is not str or not _IMAGE_RE.fullmatch(self.reference):
            _reject("broker_image_digest_required")
        if self.platform not in {"linux/arm64", "linux/amd64"}:
            _reject("broker_image_platform")
        if type(self.binary_digest) is not str or not _DIGEST_RE.fullmatch(
            self.binary_digest
        ):
            _reject("broker_binary_digest")
        _integer(
            self.protocol_version,
            "broker_protocol_version",
            BORON_PROTOCOL_VERSION,
            BORON_PROTOCOL_VERSION,
        )

    @property
    def digest(self) -> str:
        match = _IMAGE_RE.fullmatch(self.reference)
        if match is None:  # pragma: no cover - constructor invariant
            _reject("broker_image_digest_required")
        return "sha256:" + match.group("digest")


@dataclass(frozen=True, slots=True)
class BoronNetworkPlan:
    session_digest: str
    internal_network: str
    egress_network: str
    browser_container: str
    broker_container: str
    internal_subnet: str
    internal_gateway: str
    browser_internal_ip: str
    broker_internal_ip: str
    broker_alias: str = "xenon-egress"
    broker_port: int = 3128

    def __post_init__(self) -> None:
        if type(self.session_digest) is not str or not _DIGEST_RE.fullmatch(self.session_digest):
            _reject("session_digest")
        for label in (
            "internal_network",
            "egress_network",
            "browser_container",
            "broker_container",
            "broker_alias",
        ):
            _name(getattr(self, label), label)
        if self.internal_network == self.egress_network:
            _reject("network_separation")
        _integer(self.broker_port, "broker_port", 1024, 65535)
        try:
            subnet = ipaddress.ip_network(self.internal_subnet, strict=True)
            gateway = ipaddress.ip_address(self.internal_gateway)
            browser_ip = ipaddress.ip_address(self.browser_internal_ip)
            broker_ip = ipaddress.ip_address(self.broker_internal_ip)
        except ValueError:
            _reject("internal_ipam")
        if (
            subnet.version != 4
            or subnet.prefixlen != 24
            or not subnet.is_private
            or gateway not in subnet
            or browser_ip not in subnet
            or broker_ip not in subnet
            or any(
                address in {subnet.network_address, subnet.broadcast_address}
                for address in (gateway, browser_ip, broker_ip)
            )
            or len({gateway, browser_ip, broker_ip}) != 3
        ):
            _reject("internal_ipam")


@dataclass(frozen=True, slots=True)
class BoronBrowserLaunch:
    image: BoronImagePin
    network: BoronNetworkPlan
    seccomp_profile: Path
    memory_bytes: int = 1024 * 1024 * 1024
    pids_limit: int = 192
    cpu_count: float = 2.0
    profile_tmpfs_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.image) is not BoronImagePin or type(self.network) is not BoronNetworkPlan:
            _reject("launch_type")
        if (
            not isinstance(self.seccomp_profile, Path)
            or not self.seccomp_profile.is_absolute()
            or not self.seccomp_profile.is_file()
        ):
            _reject("seccomp_profile")
        _integer(self.memory_bytes, "memory_bytes", 256 * 1024 * 1024, BORON_MAX_BROWSER_MEMORY_BYTES)
        _integer(self.pids_limit, "pids_limit", 32, BORON_MAX_BROWSER_PIDS)
        if type(self.cpu_count) is not float or not 0.25 <= self.cpu_count <= 8.0:
            _reject("cpu_count")
        _integer(
            self.profile_tmpfs_bytes,
            "profile_tmpfs_bytes",
            64 * 1024 * 1024,
            1024 * 1024 * 1024,
        )

    def create_internal_network_argv(self) -> tuple[str, ...]:
        return (
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--subnet",
            self.network.internal_subnet,
            "--gateway",
            self.network.internal_gateway,
            "--label",
            "com.algo-cli.role=browser-internal",
            "--label",
            f"com.algo-cli.session={self.network.session_digest}",
            "--label",
            f"com.algo-cli.image={self.image.digest}",
            self.network.internal_network,
        )

    def browser_argv(self) -> tuple[str, ...]:
        """Return a fixed Docker launch; no caller-supplied command is accepted."""

        proxy = f"http://{self.network.broker_alias}:{self.network.broker_port}"
        tmpfs_options = "rw,noexec,nosuid,nodev,mode=0700,uid=1000,gid=1000"
        return (
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--init",
            "--name",
            self.network.browser_container,
            "--hostname",
            "boron-browser",
            "--network",
            self.network.internal_network,
            "--ip",
            self.network.browser_internal_ip,
            "--network-alias",
            "boron-browser",
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={self.seccomp_profile}",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            str(self.memory_bytes),
            "--cpus",
            str(self.cpu_count),
            "--shm-size",
            "268435456",
            "--tmpfs",
            f"/tmp:{tmpfs_options},size=134217728",
            "--tmpfs",
            f"/home/algo:{tmpfs_options},size=67108864",
            "--tmpfs",
            f"/algo-profile:{tmpfs_options},size={self.profile_tmpfs_bytes}",
            "--tmpfs",
            f"/algo-downloads:{tmpfs_options},size=16777216",
            "--env",
            f"HTTP_PROXY={proxy}",
            "--env",
            f"HTTPS_PROXY={proxy}",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=",
            "--label",
            "com.algo-cli.role=managed-browser",
            "--label",
            f"com.algo-cli.session={self.network.session_digest}",
            "--label",
            f"com.algo-cli.image={self.image.digest}",
            "--platform",
            self.image.platform,
            self.image.reference,
            "/opt/algo/bin/boron-browser-wrapper",
        )


@dataclass(frozen=True, slots=True)
class BoronBrokerLaunch:
    image: BoronBrokerImagePin
    network: BoronNetworkPlan
    seccomp_profile: Path
    memory_bytes: int = 256 * 1024 * 1024
    pids_limit: int = 48
    cpu_count: float = 1.0

    def __post_init__(self) -> None:
        if type(self.image) is not BoronBrokerImagePin or type(self.network) is not BoronNetworkPlan:
            _reject("broker_launch_type")
        if (
            not isinstance(self.seccomp_profile, Path)
            or not self.seccomp_profile.is_absolute()
            or not self.seccomp_profile.is_file()
        ):
            _reject("broker_seccomp_profile")
        _integer(
            self.memory_bytes,
            "broker_memory_bytes",
            64 * 1024 * 1024,
            BORON_MAX_BROKER_MEMORY_BYTES,
        )
        _integer(self.pids_limit, "broker_pids_limit", 8, BORON_MAX_BROKER_PIDS)
        if type(self.cpu_count) is not float or not 0.25 <= self.cpu_count <= 4.0:
            _reject("broker_cpu_count")

    def create_egress_network_argv(self) -> tuple[str, ...]:
        return (
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            "com.algo-cli.role=browser-egress",
            "--label",
            f"com.algo-cli.session={self.network.session_digest}",
            self.network.egress_network,
        )

    def broker_argv(self) -> tuple[str, ...]:
        tmpfs = "rw,noexec,nosuid,nodev,mode=0700,uid=1001,gid=1001,size=67108864"
        return (
            "docker",
            "run",
            "--detach",
            "--interactive",
            "--rm",
            "--init",
            "--name",
            self.network.broker_container,
            "--hostname",
            "xenon-broker",
            "--network",
            self.network.internal_network,
            "--ip",
            self.network.broker_internal_ip,
            "--network-alias",
            self.network.broker_alias,
            "--read-only",
            "--user",
            "1001:1001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={self.seccomp_profile}",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            str(self.memory_bytes),
            "--cpus",
            str(self.cpu_count),
            "--tmpfs",
            f"/tmp:{tmpfs}",
            "--env",
            f"XENON_LISTEN_ADDRESS={self.network.broker_internal_ip}",
            "--env",
            f"XENON_LISTEN_PORT={self.network.broker_port}",
            "--label",
            "com.algo-cli.role=egress-broker",
            "--label",
            f"com.algo-cli.session={self.network.session_digest}",
            "--label",
            f"com.algo-cli.image={self.image.digest}",
            "--label",
            f"com.algo-cli.binary={self.image.binary_digest}",
            "--platform",
            self.image.platform,
            self.image.reference,
            "/opt/algo/bin/xenon-egress-broker",
        )

    def broker_foreground_argv(self) -> tuple[str, ...]:
        """Return the same fixed broker launch with stdio attached."""

        argv = list(self.broker_argv())
        argv.remove("--detach")
        return tuple(argv)

    def connect_egress_network_argv(self) -> tuple[str, ...]:
        return (
            "docker",
            "network",
            "connect",
            self.network.egress_network,
            self.network.broker_container,
        )


@dataclass(frozen=True, slots=True)
class BoronTopologyEvidence:
    network_name: str
    egress_network_name: str
    browser_container_id: str
    broker_container_id: str
    image_digest: str
    broker_image_digest: str
    participant_count: int
    evidence_digest: str


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        _reject(label)
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if type(value) is not list:
        _reject(label)
    return value


def _first_inspect(value: Any, label: str) -> Mapping[str, Any]:
    rows = _sequence(value, label)
    if len(rows) != 1:
        _reject(label)
    return _mapping(rows[0], label)


def _container_name(row: Mapping[str, Any]) -> str:
    name = row.get("Name")
    if type(name) is not str or not name.startswith("/"):
        _reject("container_name_evidence")
    return name[1:]


def _network_members(network: Mapping[str, Any]) -> dict[str, str]:
    raw = _mapping(network.get("Containers"), "network_participants")
    members: dict[str, str] = {}
    for container_id, detail in raw.items():
        if type(container_id) is not str or not container_id:
            _reject("network_participants")
        row = _mapping(detail, "network_participants")
        name = row.get("Name")
        if type(name) is not str or not _NAME_RE.fullmatch(name):
            _reject("network_participants")
        members[name] = container_id
    return members


def _assert_no_host_surface(row: Mapping[str, Any], network_name: str, *, broker: bool) -> None:
    host = _mapping(row.get("HostConfig"), "host_config")
    config = _mapping(row.get("Config"), "container_config")
    if host.get("Privileged") is not False or host.get("ReadonlyRootfs") is not True:
        _reject("container_privilege")
    if host.get("NetworkMode") != network_name:
        _reject("broker_network_mode" if broker else "browser_network_mode")
    if host.get("PidMode") not in {"", "private"} or host.get("IpcMode") not in {"", "private"}:
        _reject("namespace_mode")
    if host.get("UTSMode") not in {"", "private"} or host.get("UsernsMode") == "host":
        _reject("namespace_mode")
    if host.get("PublishAllPorts") is not False:
        _reject("published_ports")
    bindings = host.get("PortBindings")
    if bindings not in (None, {}):
        _reject("published_ports")
    if host.get("Binds") not in (None, []):
        _reject("host_mount")
    mounts = row.get("Mounts")
    if type(mounts) is not list:
        _reject("mount_evidence")
    if broker:
        expected_tmpfs = {
            "/tmp": "rw,noexec,nosuid,nodev,mode=0700,uid=1001,gid=1001,size=67108864"
        }
    else:
        common = "rw,noexec,nosuid,nodev,mode=0700,uid=1000,gid=1000"
        expected_tmpfs = {
            "/tmp": common + ",size=134217728",
            "/home/algo": common + ",size=67108864",
            "/algo-profile": common + ",size=536870912",
            "/algo-downloads": common + ",size=16777216",
        }
    raw_tmpfs = _mapping(host.get("Tmpfs"), "tmpfs_evidence")
    if dict(raw_tmpfs) != expected_tmpfs:
        _reject("tmpfs_evidence")
    for mount in mounts:
        detail = _mapping(mount, "mount_evidence")
        if (
            detail.get("Type") != "tmpfs"
            or detail.get("Destination") not in expected_tmpfs
        ):
            _reject("host_mount")
    cap_drop = host.get("CapDrop")
    if type(cap_drop) is not list or "ALL" not in cap_drop:
        _reject("capabilities_not_dropped")
    security = host.get("SecurityOpt")
    if type(security) is not list or not any(
        item in {"no-new-privileges", "no-new-privileges=true"}
        for item in security
    ):
        _reject("no_new_privileges_missing")
    if not any(type(item) is str and item.startswith("seccomp=") for item in security):
        _reject("seccomp_missing")
    pids = host.get("PidsLimit")
    memory = host.get("Memory")
    maximum_pids = BORON_MAX_BROKER_PIDS if broker else BORON_MAX_BROWSER_PIDS
    maximum_memory = BORON_MAX_BROKER_MEMORY_BYTES if broker else BORON_MAX_BROWSER_MEMORY_BYTES
    if type(pids) is not int or not 1 <= pids <= maximum_pids:
        _reject("pids_limit_evidence")
    if type(memory) is not int or not 1 <= memory <= maximum_memory:
        _reject("memory_limit_evidence")
    user = config.get("User")
    if type(user) is not str or user in {"", "0", "root", "0:0"} or user.startswith("0:"):
        _reject("root_user")
    if host.get("AutoRemove") is not True:
        _reject("auto_remove_missing")
    restart = _mapping(host.get("RestartPolicy"), "restart_policy")
    if restart.get("Name") not in {"", "no"}:
        _reject("restart_policy")
    devices = host.get("Devices")
    if devices not in (None, []):
        _reject("device_exposure")
    device_requests = host.get("DeviceRequests")
    if device_requests not in (None, []):
        _reject("device_exposure")


def verify_docker_topology(
    plan: BoronNetworkPlan,
    image: BoronImagePin,
    broker_image: BoronBrokerImagePin,
    *,
    internal_network_json: str,
    egress_network_json: str,
    browser_inspect_json: str,
    broker_inspect_json: str,
) -> BoronTopologyEvidence:
    """Verify Docker's observed topology rather than trusting launch arguments."""

    if (
        type(plan) is not BoronNetworkPlan
        or type(image) is not BoronImagePin
        or type(broker_image) is not BoronBrokerImagePin
    ):
        _reject("topology_type")
    try:
        network = _first_inspect(json.loads(internal_network_json), "network_evidence")
        egress_network = _first_inspect(json.loads(egress_network_json), "egress_evidence")
        browser = _first_inspect(json.loads(browser_inspect_json), "browser_evidence")
        broker = _first_inspect(json.loads(broker_inspect_json), "broker_evidence")
    except (json.JSONDecodeError, UnicodeError):
        _reject("topology_json")
    if network.get("Name") != plan.internal_network:
        _reject("network_identity")
    if network.get("Driver") != "bridge" or network.get("Internal") is not True:
        _reject("network_not_internal")
    if network.get("Attachable") is not False or network.get("Ingress") is not False:
        _reject("network_exposure")
    if network.get("EnableIPv6") is not False:
        _reject("network_ipv6")
    ipam = _mapping(network.get("IPAM"), "network_ipam")
    ipam_rows = _sequence(ipam.get("Config"), "network_ipam")
    if ipam_rows != [{"Subnet": plan.internal_subnet, "Gateway": plan.internal_gateway}]:
        _reject("network_ipam")
    members = _network_members(network)
    if set(members) != {plan.browser_container, plan.broker_container}:
        _reject("network_participants")
    if _container_name(browser) != plan.browser_container:
        _reject("browser_identity")
    if _container_name(broker) != plan.broker_container:
        _reject("broker_identity")
    if (
        egress_network.get("Name") != plan.egress_network
        or egress_network.get("Driver") != "bridge"
        or egress_network.get("Internal") is not False
        or egress_network.get("Attachable") is not False
        or egress_network.get("Ingress") is not False
        or egress_network.get("EnableIPv6") is not False
    ):
        _reject("egress_network_evidence")
    egress_members = _network_members(egress_network)
    if egress_members != {plan.broker_container: members[plan.broker_container]}:
        _reject("egress_network_participants")

    _assert_no_host_surface(browser, plan.internal_network, broker=False)
    _assert_no_host_surface(broker, plan.internal_network, broker=True)

    browser_networks = _mapping(
        _mapping(browser.get("NetworkSettings"), "browser_networks").get("Networks"),
        "browser_networks",
    )
    if set(browser_networks) != {plan.internal_network}:
        _reject("browser_network_bypass")
    broker_networks = _mapping(
        _mapping(broker.get("NetworkSettings"), "broker_networks").get("Networks"),
        "broker_networks",
    )
    if set(broker_networks) != {plan.internal_network, plan.egress_network}:
        _reject("broker_network_topology")
    browser_internal = _mapping(browser_networks.get(plan.internal_network), "browser_networks")
    broker_internal = _mapping(broker_networks.get(plan.internal_network), "broker_networks")
    if browser_internal.get("IPAddress") != plan.browser_internal_ip:
        _reject("browser_internal_ip")
    if broker_internal.get("IPAddress") != plan.broker_internal_ip:
        _reject("broker_internal_ip")

    config = _mapping(browser.get("Config"), "browser_config")
    image_ref = config.get("Image")
    image_id = browser.get("Image")
    labels = _mapping(config.get("Labels"), "browser_labels")
    if image_ref != image.reference or labels.get("com.algo-cli.image") != image.digest:
        _reject("image_identity_mismatch")
    if type(image_id) is not str or not _DIGEST_RE.fullmatch(image_id):
        _reject("image_identity_mismatch")
    if config.get("Path") is not None:
        # Some Docker API versions place Path at the container root only.
        _reject("browser_command_evidence")
    if browser.get("Path") != "/opt/algo/bin/boron-browser-wrapper" or browser.get("Args") != []:
        _reject("browser_command_evidence")
    ports = _mapping(
        _mapping(browser.get("NetworkSettings"), "browser_networks").get("Ports"),
        "browser_ports",
    )
    if ports:
        _reject("published_ports")
    raw_browser_env = config.get("Env")
    if type(raw_browser_env) is not list or any(type(item) is not str for item in raw_browser_env):
        _reject("browser_proxy_environment")
    browser_env: dict[str, str] = {}
    for item in raw_browser_env:
        name, separator, value = item.partition("=")
        if not separator or name in browser_env:
            _reject("browser_proxy_environment")
        browser_env[name] = value
    proxy = f"http://{plan.broker_alias}:{plan.broker_port}"
    if (
        browser_env.get("HTTP_PROXY") != proxy
        or browser_env.get("HTTPS_PROXY") != proxy
        or browser_env.get("ALL_PROXY") != ""
        or browser_env.get("NO_PROXY") != ""
    ):
        _reject("browser_proxy_environment")

    broker_config = _mapping(broker.get("Config"), "broker_config")
    broker_labels = _mapping(broker_config.get("Labels"), "broker_labels")
    if (
        broker_config.get("Image") != broker_image.reference
        or broker_labels.get("com.algo-cli.image") != broker_image.digest
        or broker_labels.get("com.algo-cli.binary") != broker_image.binary_digest
        or broker.get("Path") != "/opt/algo/bin/xenon-egress-broker"
        or broker.get("Args") != []
    ):
        _reject("broker_image_identity_mismatch")
    broker_image_id = broker.get("Image")
    if type(broker_image_id) is not str or not _DIGEST_RE.fullmatch(broker_image_id):
        _reject("broker_image_identity_mismatch")
    raw_broker_env = broker_config.get("Env")
    if type(raw_broker_env) is not list or any(type(item) is not str for item in raw_broker_env):
        _reject("broker_environment")
    broker_env: dict[str, str] = {}
    for item in raw_broker_env:
        name, separator, value = item.partition("=")
        if not separator or name in broker_env:
            _reject("broker_environment")
        broker_env[name] = value
    if (
        broker_env.get("XENON_LISTEN_ADDRESS") != plan.broker_internal_ip
        or broker_env.get("XENON_LISTEN_PORT") != str(plan.broker_port)
        or any(
            broker_env.get(name, "")
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        )
    ):
        _reject("broker_environment")

    evidence = {
        "network": plan.internal_network,
        "members": sorted(members),
        "browser": members[plan.browser_container],
        "broker": members[plan.broker_container],
        "image_manifest": image.digest,
        "image_id": image_id,
        "broker_image_manifest": broker_image.digest,
        "broker_image_id": broker_image_id,
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return BoronTopologyEvidence(
        network_name=plan.internal_network,
        egress_network_name=plan.egress_network,
        browser_container_id=members[plan.browser_container],
        broker_container_id=members[plan.broker_container],
        image_digest=image.digest,
        broker_image_digest=broker_image.digest,
        participant_count=len(members),
        evidence_digest=digest,
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def probe_docker_image(
    image: BoronImagePin,
    *,
    runner: Runner = subprocess.run,
) -> BoronReadinessState:
    """Perform a bounded local Docker daemon, seccomp, and image identity probe."""

    if type(image) is not BoronImagePin:
        _reject("image_type")
    try:
        version = runner(
            ["docker", "version", "--format", "{{json .Server}}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except FileNotFoundError:
        return BoronReadinessState.DOCKER_NOT_INSTALLED
    except (OSError, subprocess.TimeoutExpired):
        return BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE
    if version.returncode != 0:
        return BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE
    try:
        server = json.loads(version.stdout)
    except json.JSONDecodeError:
        return BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE
    if type(server) is not dict or server.get("Os") != "linux":
        return BoronReadinessState.DOCKER_SECURITY_UNAVAILABLE

    info = runner(
        ["docker", "info", "--format", "{{json .SecurityOptions}}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if info.returncode != 0:
        return BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE
    try:
        security = json.loads(info.stdout)
    except json.JSONDecodeError:
        return BoronReadinessState.DOCKER_SECURITY_UNAVAILABLE
    if type(security) is not list or not any("seccomp" in str(item) for item in security):
        return BoronReadinessState.DOCKER_SECURITY_UNAVAILABLE

    inspect = runner(
        ["docker", "image", "inspect", image.reference, "--format", "{{json .RepoDigests}}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if inspect.returncode != 0:
        return BoronReadinessState.IMAGE_NOT_INSTALLED
    try:
        digests = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        return BoronReadinessState.IMAGE_IDENTITY_MISMATCH
    if type(digests) is not list or image.reference not in digests:
        return BoronReadinessState.IMAGE_IDENTITY_MISMATCH
    return BoronReadinessState.READY


__all__ = [
    "BORON_MAX_BROWSER_MEMORY_BYTES",
    "BORON_MAX_BROWSER_PIDS",
    "BORON_MAX_BROKER_MEMORY_BYTES",
    "BORON_MAX_BROKER_PIDS",
    "BORON_MAX_SECURITY_LAG_MS",
    "BORON_PROTOCOL_VERSION",
    "BoronBrowserFamily",
    "BoronBrowserLaunch",
    "BoronBrokerImagePin",
    "BoronBrokerLaunch",
    "BoronImagePin",
    "BoronImagePurpose",
    "BoronIsolationRejected",
    "BoronNetworkPlan",
    "BoronReadinessState",
    "BoronTopologyEvidence",
    "probe_docker_image",
    "verify_docker_topology",
]
