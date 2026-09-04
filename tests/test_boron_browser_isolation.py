from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from algo_cli.boron_browser_isolation import (
    BORON_MAX_BROWSER_MEMORY_BYTES,
    BORON_MAX_RELEASE_EVIDENCE_AGE_MS,
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
    BoronReadinessState,
    BoronReleaseEvidenceSource,
    probe_docker_image,
    verify_docker_topology,
)


ROOT = Path(__file__).resolve().parents[1]
NOW_MS = 1_800_000_000_000
DIGEST = "1" * 64
IMAGE_ID = "sha256:" + "2" * 64
BROKER_IMAGE_ID = "sha256:" + "6" * 64
PUBLIC_REF = f"registry.example/algo/boron-browser@sha256:{DIGEST}"
FIXTURE_REF = f"mcr.microsoft.com/playwright@sha256:{'3' * 64}"
BROKER_REF = f"registry.example/algo/xenon-broker@sha256:{'5' * 64}"
BROKER_BINARY_DIGEST = "sha256:" + "7" * 64


def _live_module():
    path = ROOT / "scripts/boron_browser_live_session.py"
    scripts_path = str(path.parent)
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location("boron_browser_live_session", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts_path)


def _public_image() -> BoronImagePin:
    return BoronImagePin(
        PUBLIC_REF,
        BoronImagePurpose.PUBLIC_MANAGED,
        BoronBrowserFamily.CHROMIUM_STABLE,
        "151.0.7922.34",
        "linux/arm64",
        NOW_MS - 60_000,
    )


def _fixture_image() -> BoronImagePin:
    return BoronImagePin(
        FIXTURE_REF,
        BoronImagePurpose.TRUSTED_FIXTURE,
        BoronBrowserFamily.CHROME_FOR_TESTING,
        "151.0.7922.34",
        "linux/arm64",
        NOW_MS - 60_000,
    )


def _chrome_image(
    *,
    version: str = "150.0.7871.128",
    release_at_ms: int = NOW_MS - 30 * 86_400_000,
    platform: str = "linux/amd64",
) -> BoronImagePin:
    return BoronImagePin(
        f"registry.example/algo/chrome@sha256:{DIGEST}",
        BoronImagePurpose.PUBLIC_MANAGED,
        BoronBrowserFamily.CHROME_STABLE,
        version,
        platform,
        release_at_ms,
    )


def _release_evidence(
    *,
    version: str = "150.0.7871.128",
    release_at_ms: int = NOW_MS - 30 * 86_400_000,
    observed_at_ms: int = NOW_MS,
) -> BoronBrowserReleaseEvidence:
    return BoronBrowserReleaseEvidence(
        BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY,
        BoronBrowserFamily.CHROME_STABLE,
        version,
        "linux/amd64",
        release_at_ms,
        observed_at_ms,
        "sha256:" + "8" * 64,
    )


def _broker_image() -> BoronBrokerImagePin:
    return BoronBrokerImagePin(
        BROKER_REF,
        "linux/arm64",
        BROKER_BINARY_DIGEST,
    )


def _plan() -> BoronNetworkPlan:
    return BoronNetworkPlan(
        session_digest="sha256:" + "4" * 64,
        internal_network="boron-private-a1",
        egress_network="xenon-egress-a1",
        browser_container="boron-browser-a1",
        broker_container="xenon-broker-a1",
        internal_subnet="172.30.91.0/24",
        internal_gateway="172.30.91.1",
        browser_internal_ip="172.30.91.2",
        broker_internal_ip="172.30.91.3",
    )


def _host_config(network_mode: str, *, broker: bool = False) -> dict[str, Any]:
    if broker:
        tmpfs = {"/tmp": "rw,noexec,nosuid,nodev,mode=0700,uid=1001,gid=1001,size=67108864"}
    else:
        common = "rw,noexec,nosuid,nodev,mode=0700,uid=1000,gid=1000"
        tmpfs = {
            "/tmp": common + ",size=134217728",
            "/home/algo": common + ",size=67108864",
            "/algo-profile": common + ",size=536870912",
            "/algo-downloads": common + ",size=16777216",
        }
    return {
        "Privileged": False,
        "ReadonlyRootfs": True,
        "NetworkMode": network_mode,
        "PidMode": "",
        "IpcMode": "private",
        "UTSMode": "",
        "UsernsMode": "",
        "PublishAllPorts": False,
        "PortBindings": {},
        "Binds": None,
        "CapDrop": ["ALL"],
        "SecurityOpt": [
            "no-new-privileges=true",
            "seccomp=/algo/boron_seccomp_profile.json",
        ],
        "PidsLimit": 48 if broker else 192,
        "Memory": 256 * 1024 * 1024 if broker else 1024 * 1024 * 1024,
        "AutoRemove": True,
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Devices": [],
        "DeviceRequests": [],
        "Tmpfs": tmpfs,
    }


def _topology_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    plan = _plan()
    image = _public_image()
    browser_id = "a" * 64
    broker_id = "b" * 64
    network = [
        {
            "Name": plan.internal_network,
            "Labels": {
                "com.algo-cli.role": "browser-internal",
                "com.algo-cli.session": plan.session_digest,
            },
            "Driver": "bridge",
            "Internal": True,
            "Attachable": False,
            "Ingress": False,
            "EnableIPv6": False,
            "IPAM": {
                "Config": [
                    {
                        "Subnet": plan.internal_subnet,
                        "Gateway": plan.internal_gateway,
                    }
                ]
            },
            "Containers": {
                browser_id: {"Name": plan.browser_container},
                broker_id: {"Name": plan.broker_container},
            },
        }
    ]
    browser = [
        {
            "Name": "/" + plan.browser_container,
            "Image": IMAGE_ID,
            "Path": "/opt/algo/bin/boron-browser-wrapper",
            "Args": [],
            "Config": {
                "User": "1000:1000",
                "Image": image.reference,
                "Labels": {
                    "com.algo-cli.role": "managed-browser",
                    "com.algo-cli.session": plan.session_digest,
                    "com.algo-cli.image": image.digest,
                },
                "Env": [
                    f"HTTP_PROXY=http://{plan.broker_alias}:{plan.broker_port}",
                    f"HTTPS_PROXY=http://{plan.broker_alias}:{plan.broker_port}",
                    "ALL_PROXY=",
                    "NO_PROXY=",
                ],
            },
            "HostConfig": _host_config(plan.internal_network),
            "Mounts": [],
            "NetworkSettings": {
                "Networks": {plan.internal_network: {"IPAddress": plan.browser_internal_ip}},
                "Ports": {},
            },
        }
    ]
    broker = [
        {
            "Name": "/" + plan.broker_container,
            "Image": BROKER_IMAGE_ID,
            "Path": "/opt/algo/bin/xenon-egress-broker",
            "Args": [],
            "Config": {
                "User": "1001:1001",
                "Image": _broker_image().reference,
                "Labels": {
                    "com.algo-cli.role": "egress-broker",
                    "com.algo-cli.session": plan.session_digest,
                    "com.algo-cli.image": _broker_image().digest,
                    "com.algo-cli.binary": _broker_image().binary_digest,
                },
                "Env": [
                    f"XENON_LISTEN_ADDRESS={plan.broker_internal_ip}",
                    f"XENON_LISTEN_PORT={plan.broker_port}",
                ],
            },
            "HostConfig": _host_config(plan.internal_network, broker=True),
            "Mounts": [],
            "NetworkSettings": {
                "Networks": {
                    plan.internal_network: {"IPAddress": plan.broker_internal_ip},
                    plan.egress_network: {},
                },
                "Ports": {},
            },
        }
    ]
    return network, browser, broker


def _egress_rows() -> list[dict[str, Any]]:
    plan = _plan()
    return [
        {
            "Name": plan.egress_network,
            "Labels": {
                "com.algo-cli.role": "browser-egress",
                "com.algo-cli.session": plan.session_digest,
            },
            "Driver": "bridge",
            "Internal": False,
            "Attachable": False,
            "Ingress": False,
            "EnableIPv6": False,
            "Containers": {
                "b" * 64: {"Name": plan.broker_container},
            },
        }
    ]


def _verify(
    network: list[dict[str, Any]],
    browser: list[dict[str, Any]],
    broker: list[dict[str, Any]],
    egress: list[dict[str, Any]] | None = None,
):
    return verify_docker_topology(
        _plan(),
        _public_image(),
        _broker_image(),
        browser_runtime_image_id=IMAGE_ID,
        broker_runtime_image_id=BROKER_IMAGE_ID,
        internal_network_json=json.dumps(network),
        egress_network_json=json.dumps(egress or _egress_rows()),
        browser_inspect_json=json.dumps(browser),
        broker_inspect_json=json.dumps(broker),
    )


def test_public_and_fixture_images_are_type_separated_and_digest_pinned() -> None:
    assert _public_image().purpose is BoronImagePurpose.PUBLIC_MANAGED
    assert _fixture_image().purpose is BoronImagePurpose.TRUSTED_FIXTURE
    with pytest.raises(BoronIsolationRejected, match="image_digest_required"):
        BoronImagePin(
            "registry.example/algo/browser:latest",
            BoronImagePurpose.PUBLIC_MANAGED,
            BoronBrowserFamily.CHROME_STABLE,
            "151.0.7922.34",
            "linux/arm64",
            NOW_MS,
        )
    with pytest.raises(BoronIsolationRejected, match="testing_browser_public_route"):
        BoronImagePin(
            f"registry.example/cft@sha256:{DIGEST}",
            BoronImagePurpose.PUBLIC_MANAGED,
            BoronBrowserFamily.CHROME_FOR_TESTING,
            "151.0.7922.34",
            "linux/arm64",
            NOW_MS,
        )
    with pytest.raises(BoronIsolationRejected, match="testing_image_public_route"):
        BoronImagePin(
            f"mcr.microsoft.com/playwright@sha256:{DIGEST}",
            BoronImagePurpose.PUBLIC_MANAGED,
            BoronBrowserFamily.CHROMIUM_STABLE,
            "151.0.7922.34",
            "linux/arm64",
            NOW_MS,
        )
    with pytest.raises(BoronIsolationRejected, match="fixture_browser_family"):
        BoronImagePin(
            f"registry.example/fixture@sha256:{DIGEST}",
            BoronImagePurpose.TRUSTED_FIXTURE,
            BoronBrowserFamily.CHROMIUM_STABLE,
            "151.0.7922.34",
            "linux/arm64",
            NOW_MS,
        )


def test_current_public_browser_passes_regardless_of_release_age() -> None:
    image = _chrome_image()
    evidence = _release_evidence()
    assert (
        image.security_update_lag_ms(
            now_ms=NOW_MS,
            release_evidence=evidence,
        )
        == 0
    )
    image.assert_fresh(now_ms=NOW_MS, release_evidence=evidence)
    # Fixture age does not become a public-browser claim.
    _fixture_image().assert_fresh(now_ms=NOW_MS + 365 * 86_400_000)


def test_superseded_public_browser_has_a_72_hour_update_lag_gate() -> None:
    image = _chrome_image(
        version="150.0.7871.127",
        release_at_ms=NOW_MS - 40 * 86_400_000,
    )
    boundary_evidence = _release_evidence(
        release_at_ms=NOW_MS - BORON_MAX_SECURITY_LAG_MS,
    )
    assert (
        image.security_update_lag_ms(
            now_ms=NOW_MS,
            release_evidence=boundary_evidence,
        )
        == BORON_MAX_SECURITY_LAG_MS
    )
    with pytest.raises(BoronIsolationRejected, match="browser_security_update_stale"):
        image.assert_fresh(
            now_ms=NOW_MS,
            release_evidence=_release_evidence(
                release_at_ms=NOW_MS - BORON_MAX_SECURITY_LAG_MS - 1,
            ),
        )


def test_public_browser_requires_current_matching_authoritative_evidence() -> None:
    image = _chrome_image()
    with pytest.raises(BoronIsolationRejected, match="browser_security_evidence_required"):
        image.assert_fresh(now_ms=NOW_MS)
    with pytest.raises(BoronIsolationRejected, match="browser_security_evidence_stale"):
        image.assert_fresh(
            now_ms=NOW_MS,
            release_evidence=_release_evidence(
                observed_at_ms=NOW_MS - BORON_MAX_RELEASE_EVIDENCE_AGE_MS - 1,
            ),
        )
    with pytest.raises(BoronIsolationRejected, match="browser_security_evidence_mismatch"):
        _chrome_image(platform="linux/arm64").assert_fresh(
            now_ms=NOW_MS,
            release_evidence=_release_evidence(),
        )
    with pytest.raises(BoronIsolationRejected, match="browser_security_feed_regression"):
        _chrome_image(version="150.0.7871.129").assert_fresh(
            now_ms=NOW_MS,
            release_evidence=_release_evidence(),
        )
    with pytest.raises(
        BoronIsolationRejected,
        match="browser_security_release_timestamp_mismatch",
    ):
        image.assert_fresh(
            now_ms=NOW_MS,
            release_evidence=_release_evidence(
                release_at_ms=image.security_release_at_ms + 1,
            ),
        )


def test_launch_argv_has_no_host_mount_port_root_or_floating_image() -> None:
    seccomp = (ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json").resolve()
    launch = BoronBrowserLaunch(_public_image(), _plan(), seccomp)
    argv = launch.browser_argv()
    rendered = " ".join(argv)
    assert argv[:4] == ("docker", "run", "--rm", "--interactive")
    assert "--read-only" in argv
    assert ("--user", "1000:1000") == argv[argv.index("--user") : argv.index("--user") + 2]
    assert ("--cap-drop", "ALL") == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "no-new-privileges=true" in argv
    assert f"seccomp={seccomp}" in argv
    assert "--publish" not in argv and "-p" not in argv
    assert "--volume" not in argv and "-v" not in argv and "--mount" not in argv
    assert "--privileged" not in argv
    assert "--network" in argv and _plan().internal_network in argv
    assert ("--ip", _plan().browser_internal_ip) == argv[argv.index("--ip") : argv.index("--ip") + 2]
    assert "NO_PROXY=" in argv and "ALL_PROXY=" in argv
    assert "com.algo-cli.role=managed-browser" in argv
    assert f"com.algo-cli.session={_plan().session_digest}" in argv
    assert sum("uid=1000,gid=1000" in item for item in argv) == 4
    assert _public_image().reference in argv
    assert ":latest" not in rendered
    assert argv[-1] == "/opt/algo/bin/boron-browser-wrapper"


def test_internal_network_creation_has_no_host_or_attachable_route() -> None:
    launch = BoronBrowserLaunch(
        _public_image(),
        _plan(),
        (ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json").resolve(),
    )
    argv = launch.create_internal_network_argv()
    assert "--internal" in argv
    assert "com.algo-cli.role=browser-internal" in argv
    assert f"com.algo-cli.session={_plan().session_digest}" in argv
    assert ("--subnet", _plan().internal_subnet) == argv[argv.index("--subnet") : argv.index("--subnet") + 2]
    assert "--attachable" not in argv
    assert argv[-1] == _plan().internal_network


def test_broker_launch_is_digest_pinned_private_and_has_no_host_surface() -> None:
    launch = BoronBrokerLaunch(
        _broker_image(),
        _plan(),
        (ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json").resolve(),
    )
    argv = launch.broker_argv()
    rendered = " ".join(argv)
    assert argv[:4] == ("docker", "run", "--detach", "--interactive")
    assert ("--network", _plan().internal_network) == argv[argv.index("--network") : argv.index("--network") + 2]
    assert ("--ip", _plan().broker_internal_ip) == argv[argv.index("--ip") : argv.index("--ip") + 2]
    assert "--read-only" in argv
    assert ("--cap-drop", "ALL") == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "no-new-privileges=true" in argv
    assert "--publish" not in argv and "--volume" not in argv and "--mount" not in argv
    assert ":latest" not in rendered
    assert _broker_image().reference in argv
    assert _broker_image().binary_digest in rendered
    assert "com.algo-cli.role=egress-broker" in argv
    assert f"com.algo-cli.session={_plan().session_digest}" in argv
    assert argv[-1] == "/opt/algo/bin/xenon-egress-broker"
    egress_argv = launch.create_egress_network_argv()
    assert "--internal" not in egress_argv
    assert "com.algo-cli.role=browser-egress" in egress_argv
    assert f"com.algo-cli.session={_plan().session_digest}" in egress_argv
    assert launch.connect_egress_network_argv()[-2:] == (
        _plan().egress_network,
        _plan().broker_container,
    )
    foreground = launch.broker_foreground_argv()
    assert "--detach" not in foreground
    assert foreground[:3] == ("docker", "run", "--interactive")
    assert foreground[-1] == "/opt/algo/bin/xenon-egress-broker"
    assert set(foreground) == set(argv) - {"--detach"}


def test_observed_topology_passes_and_returns_structural_evidence() -> None:
    evidence = _verify(*_topology_rows())
    assert evidence.network_name == _plan().internal_network
    assert evidence.egress_network_name == _plan().egress_network
    assert evidence.participant_count == 2
    assert evidence.image_digest == _public_image().digest
    assert evidence.broker_image_digest == _broker_image().digest
    assert evidence.evidence_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda n, b, r: n[0].__setitem__("Internal", False), "network_not_internal"),
        (lambda n, b, r: n[0].__setitem__("Attachable", True), "network_exposure"),
        (
            lambda n, b, r: n[0]["Labels"].__setitem__("com.algo-cli.session", "sha256:" + "9" * 64),
            "network_ownership",
        ),
        (
            lambda n, b, r: n[0]["Containers"].__setitem__("c" * 64, {"Name": "intruder"}),
            "network_participants",
        ),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Privileged", True), "container_privilege"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("ReadonlyRootfs", False), "container_privilege"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("NetworkMode", "bridge"), "browser_network_mode"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("PidMode", "host"), "namespace_mode"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("IpcMode", "host"), "namespace_mode"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("UsernsMode", "host"), "namespace_mode"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("PublishAllPorts", True), "published_ports"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Binds", ["/Users:/host"]), "host_mount"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Tmpfs", {}), "tmpfs_evidence"),
        (lambda n, b, r: b[0]["HostConfig"]["Tmpfs"].__setitem__("/tmp", "rw,size=1"), "tmpfs_evidence"),
        (lambda n, b, r: b[0].__setitem__("Mounts", [{"Type": "bind", "Destination": "/host"}]), "host_mount"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("CapDrop", []), "capabilities_not_dropped"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("SecurityOpt", ["seccomp=x"]), "no_new_privileges_missing"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("SecurityOpt", ["no-new-privileges=true"]), "seccomp_missing"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("PidsLimit", 0), "pids_limit_evidence"),
        (
            lambda n, b, r: b[0]["HostConfig"].__setitem__("Memory", BORON_MAX_BROWSER_MEMORY_BYTES + 1),
            "memory_limit_evidence",
        ),
        (lambda n, b, r: b[0]["Config"].__setitem__("User", "0:0"), "root_user"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("AutoRemove", False), "auto_remove_missing"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Devices", [{"PathOnHost": "/dev/x"}]), "device_exposure"),
        (
            lambda n, b, r: b[0]["NetworkSettings"].__setitem__(
                "Networks", {_plan().internal_network: {}, "bridge": {}}
            ),
            "browser_network_bypass",
        ),
        (
            lambda n, b, r: r[0]["NetworkSettings"].__setitem__("Networks", {_plan().internal_network: {}}),
            "broker_network_topology",
        ),
        (lambda n, b, r: b[0]["Config"].__setitem__("Image", "other@sha256:" + "9" * 64), "image_identity_mismatch"),
        (lambda n, b, r: b[0].__setitem__("Image", "sha256:" + "9" * 64), "image_identity_mismatch"),
        (
            lambda n, b, r: b[0]["Config"]["Labels"].__setitem__("com.algo-cli.session", "sha256:" + "9" * 64),
            "image_identity_mismatch",
        ),
        (lambda n, b, r: b[0].__setitem__("Path", "/bin/sh"), "browser_command_evidence"),
        (lambda n, b, r: r[0].__setitem__("Path", "/bin/sh"), "broker_image_identity_mismatch"),
        (
            lambda n, b, r: r[0]["Config"].__setitem__("Image", "other@sha256:" + "9" * 64),
            "broker_image_identity_mismatch",
        ),
        (lambda n, b, r: r[0].__setitem__("Image", "sha256:" + "9" * 64), "broker_image_identity_mismatch"),
        (
            lambda n, b, r: r[0]["Config"]["Labels"].__setitem__("com.algo-cli.session", "sha256:" + "9" * 64),
            "broker_image_identity_mismatch",
        ),
        (lambda n, b, r: b[0]["Config"].__setitem__("Env", ["HTTP_PROXY=http://evil"]), "browser_proxy_environment"),
        (
            lambda n, b, r: r[0]["Config"].__setitem__(
                "Env", ["XENON_LISTEN_ADDRESS=0.0.0.0", "XENON_LISTEN_PORT=3128"]
            ),
            "broker_environment",
        ),
        (lambda n, b, r: r[0]["HostConfig"].__setitem__("NetworkMode", "bridge"), "broker_network_mode"),
        (
            lambda n, b, r: r[0].__setitem__(
                "Mounts", [{"Type": "tmpfs", "Destination": "/tmp"}, {"Type": "tmpfs", "Destination": "/algo-profile"}]
            ),
            "host_mount",
        ),
    ],
)
def test_each_topology_escape_or_identity_drift_is_rejected(mutator, reason: str) -> None:
    network, browser, broker = deepcopy(_topology_rows())
    mutator(network, browser, broker)
    with pytest.raises(BoronIsolationRejected, match=reason):
        _verify(network, browser, broker)


def test_egress_network_must_be_external_and_broker_only() -> None:
    network, browser, broker = _topology_rows()
    egress = deepcopy(_egress_rows())
    egress[0]["Internal"] = True
    with pytest.raises(BoronIsolationRejected, match="egress_network_evidence"):
        _verify(network, browser, broker, egress)
    egress = deepcopy(_egress_rows())
    egress[0]["Containers"]["c" * 64] = {"Name": "intruder"}
    with pytest.raises(BoronIsolationRejected, match="egress_network_participants"):
        _verify(network, browser, broker, egress)
    egress = deepcopy(_egress_rows())
    egress[0]["Labels"]["com.algo-cli.session"] = "sha256:" + "9" * 64
    with pytest.raises(BoronIsolationRejected, match="egress_network_ownership"):
        _verify(network, browser, broker, egress)


def test_docker_probe_distinguishes_missing_daemon_security_image_and_identity() -> None:
    image = _public_image()

    class Runner:
        def __init__(self, outputs: list[tuple[int, str]]) -> None:
            self.outputs = outputs

        def __call__(self, *_args, **_kwargs):
            code, stdout = self.outputs.pop(0)
            return subprocess.CompletedProcess([], code, stdout, "failure")

    ready = Runner(
        [
            (0, json.dumps({"Os": "linux"})),
            (0, json.dumps(["name=seccomp,profile=builtin", "name=cgroupns"])),
            (0, json.dumps([image.reference])),
        ]
    )
    assert probe_docker_image(image, runner=ready) is BoronReadinessState.READY

    daemon = Runner([(1, "")])
    assert probe_docker_image(image, runner=daemon) is BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE

    security = Runner([(0, json.dumps({"Os": "linux"})), (0, json.dumps(["name=cgroupns"]))])
    assert probe_docker_image(image, runner=security) is BoronReadinessState.DOCKER_SECURITY_UNAVAILABLE

    missing = Runner(
        [
            (0, json.dumps({"Os": "linux"})),
            (0, json.dumps(["name=seccomp"])),
            (1, ""),
        ]
    )
    assert probe_docker_image(image, runner=missing) is BoronReadinessState.IMAGE_NOT_INSTALLED

    mismatch = Runner(
        [
            (0, json.dumps({"Os": "linux"})),
            (0, json.dumps(["name=seccomp"])),
            (0, json.dumps(["other@sha256:" + "8" * 64])),
        ]
    )
    assert probe_docker_image(image, runner=mismatch) is BoronReadinessState.IMAGE_IDENTITY_MISMATCH


def test_managed_policy_disables_high_risk_browser_surfaces() -> None:
    policy_path = ROOT / "algo_cli/resources/boron_browser/boron_managed_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert policy["DownloadRestrictions"] == 3
    assert policy["AllowFileSelectionDialogs"] is False
    assert policy["IncognitoModeAvailability"] == 1
    assert policy["PasswordManagerEnabled"] is False
    assert policy["SyncDisabled"] is True
    assert policy["ExtensionInstallBlocklist"] == ["*"]
    assert policy["DefaultPopupsSetting"] == 2
    assert policy["QuicAllowed"] is False
    assert {"chrome://*", "chrome-untrusted://*", "devtools://*", "file://*"} <= set(policy["URLBlocklist"])


def test_seccomp_profile_is_deny_by_default_and_only_adds_browser_namespace_calls() -> None:
    profile_path = ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    namespace_rows = [
        row
        for row in profile["syscalls"]
        if row.get("comment") == "Allow browser user namespaces and their sandbox chroot"
    ]
    assert len(namespace_rows) == 1
    assert set(namespace_rows[0]["names"]) == {"chroot", "clone", "setns", "unshare"}
    assert namespace_rows[0]["action"] == "SCMP_ACT_ALLOW"


class _CleanupStream:
    def __init__(self, name: str, events: list[str], failures: set[str]) -> None:
        self.name = name
        self.events = events
        self.failures = failures
        self.closed = False

    def close(self) -> None:
        self.events.append(self.name)
        self.closed = True
        if self.name in self.failures:
            raise RuntimeError("sensitive stream detail")


class _CleanupSelector:
    def __init__(self, events: list[str], failures: set[str]) -> None:
        self.events = events
        self.failures = failures
        self.closed = False

    def register(self, _stream, _events) -> None:
        self.events.append("selector_register")
        if "selector_register" in self.failures:
            raise RuntimeError("sensitive selector detail")

    def close(self) -> None:
        self.events.append("selector_close")
        self.closed = True
        if "selector_close" in self.failures:
            raise RuntimeError("sensitive selector detail")


class _CleanupThread:
    def __init__(self, events: list[str], failures: set[str]) -> None:
        self.events = events
        self.failures = failures
        self.started = False

    def start(self) -> None:
        self.events.append("thread_start")
        if "thread_start" in self.failures:
            raise RuntimeError("sensitive thread detail")
        self.started = True

    def join(self, *, timeout: float) -> None:
        assert timeout > 0
        self.events.append("thread_join")
        if "thread_join" in self.failures:
            raise RuntimeError("sensitive thread detail")

    def is_alive(self) -> bool:
        self.events.append("thread_state")
        if "thread_state" in self.failures:
            raise RuntimeError("sensitive thread detail")
        return "thread_alive" in self.failures


class _CleanupProcess:
    def __init__(
        self,
        events: list[str],
        failures: set[str],
        *,
        missing_stdout: bool = False,
    ) -> None:
        self.events = events
        self.failures = failures
        self.stdin = _CleanupStream("stdin_close", events, failures)
        self.stdout = None if missing_stdout else _CleanupStream("stdout_close", events, failures)
        self.stderr = _CleanupStream("stderr_close", events, failures)
        self.stopped = False
        self.poll_count = 0
        self.wait_count = 0

    def poll(self) -> int | None:
        self.poll_count += 1
        boundary = "process_poll_initial" if self.poll_count == 1 else "process_poll_final"
        self.events.append(boundary)
        if boundary in self.failures:
            raise RuntimeError("sensitive process detail")
        return 0 if self.stopped else None

    def terminate(self) -> None:
        self.events.append("process_terminate")
        if "process_terminate" in self.failures:
            raise RuntimeError("sensitive process detail")

    def wait(self, *, timeout: float) -> int:
        assert timeout > 0
        self.wait_count += 1
        boundary = "process_wait" if self.wait_count == 1 else "process_kill_wait"
        self.events.append(boundary)
        if boundary in self.failures:
            raise subprocess.TimeoutExpired("redacted", timeout)
        self.stopped = True
        return 0

    def kill(self) -> None:
        self.events.append("process_kill")
        if "process_kill" in self.failures:
            raise RuntimeError("sensitive process detail")
        self.stopped = True


@pytest.mark.parametrize(
    ("failed_action", "expected_reason"),
    [
        ("browser_container", "browser_container_cleanup_failed"),
        ("broker_container", "broker_container_cleanup_failed"),
        ("browser_driver", "browser_driver_cleanup_failed"),
        ("broker_driver", "broker_driver_cleanup_failed"),
        ("egress_network", "egress_network_cleanup_failed"),
        ("internal_network", "internal_network_cleanup_failed"),
    ],
)
def test_live_cleanup_isolates_every_resource_boundary(
    monkeypatch,
    failed_action: str,
    expected_reason: str,
) -> None:
    module = _live_module()
    plan = _plan()
    events: list[str] = []

    def cleanup_container(name: str, **_kwargs) -> bool:
        action = "browser_container" if name == plan.browser_container else "broker_container"
        events.append(action)
        if action == failed_action:
            raise RuntimeError("sensitive container detail")
        return True

    def cleanup_network(name: str, **_kwargs) -> bool:
        action = "egress_network" if name == plan.egress_network else "internal_network"
        events.append(action)
        if action == failed_action:
            raise RuntimeError("sensitive network detail")
        return True

    class Driver:
        def __init__(self, action: str) -> None:
            self.action = action

        def close(self) -> bool:
            events.append(self.action)
            if self.action == failed_action:
                raise RuntimeError("sensitive driver detail")
            return True

    monkeypatch.setattr(module, "_cleanup_container", cleanup_container)
    monkeypatch.setattr(module, "_cleanup_network", cleanup_network)
    failures = module._cleanup_live_resources(
        plan,
        browser_process=Driver("browser_driver"),
        broker_process=Driver("broker_driver"),
        attempted_resources=module._CLEANUP_RESOURCE_KEYS,
    )
    assert events == [
        "browser_driver",
        "broker_driver",
        "browser_container",
        "broker_container",
        "egress_network",
        "internal_network",
    ]
    assert failures == (expected_reason,)
    assert "sensitive" not in repr(failures)


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        ("container", "managed-browser"),
        ("network", "browser-internal"),
    ],
)
def test_foreign_cleanup_collision_is_inspected_but_never_mutated(
    monkeypatch,
    kind: str,
    role: str,
) -> None:
    module = _live_module()
    resource_id = "a" * 64
    commands: list[list[str]] = []
    foreign = json.dumps(
        {
            "id": resource_id,
            "labels": {
                "com.algo-cli.role": role,
                "com.algo-cli.session": "sha256:" + "9" * 64,
            },
        }
    )

    def fake_run(args, **_kwargs):
        commands.append(args)
        return module.subprocess.CompletedProcess([], 0, foreign, "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    cleanup = module._cleanup_container if kind == "container" else module._cleanup_network
    assert (
        cleanup(
            "colliding-name",
            session_digest=_plan().session_digest,
            role=role,
        )
        is False
    )
    assert len(commands) == 1
    assert commands[0][:4] == ["docker", kind, "inspect", "colliding-name"]
    assert all("stop" not in command and "rm" not in command for command in commands)


def test_owned_container_stop_timeout_uses_verified_id_force_remove(
    monkeypatch,
) -> None:
    module = _live_module()
    resource_id = "a" * 64
    session_digest = _plan().session_digest
    commands: list[list[str]] = []
    owned = json.dumps(
        {
            "id": resource_id,
            "labels": {
                "com.algo-cli.role": "managed-browser",
                "com.algo-cli.session": session_digest,
            },
        }
    )

    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["docker", "stop"]:
            raise subprocess.TimeoutExpired(args, 5)
        if args[:4] == ["docker", "container", "inspect", "browser"]:
            return module.subprocess.CompletedProcess([], 0, owned, "")
        if args[:4] == ["docker", "container", "inspect", resource_id]:
            return module.subprocess.CompletedProcess([], 0, owned, "")
        if args[:4] == ["docker", "container", "rm", "--force"]:
            return module.subprocess.CompletedProcess([], 0, "", "")
        raise AssertionError(args)

    absence = iter((False, True))
    observed_absence: list[tuple[str, str]] = []

    def wait_absent(kind: str, identifier: str) -> bool:
        observed_absence.append((kind, identifier))
        return next(absence)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_wait_docker_absent", wait_absent)
    assert (
        module._cleanup_container(
            "browser",
            session_digest=session_digest,
            role="managed-browser",
        )
        is True
    )
    assert ["docker", "stop", "--signal", "TERM", "--time", "3", resource_id] in commands
    assert ["docker", "container", "rm", "--force", resource_id] in commands
    assert observed_absence == [("container", resource_id), ("container", resource_id)]
    assert all("browser" not in command for command in commands[1:])


def test_cleanup_waits_for_late_owned_resource_before_mutation(monkeypatch) -> None:
    module = _live_module()
    resource_id = "a" * 64
    clock = [100.0]
    identity_calls: list[tuple[str, str, float]] = []
    mutations: list[list[str]] = []

    def identity(
        kind: str,
        identifier: str,
        *,
        session_digest: str,
        role: str,
        timeout_seconds: float,
    ):
        assert session_digest == _plan().session_digest
        assert role == "browser-internal"
        identity_calls.append((kind, identifier, timeout_seconds))
        if len(identity_calls) == 1:
            return "absent", None
        return "owned", resource_id

    def fake_run(args, **_kwargs):
        mutations.append(args)
        return module.subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(module, "CLEANUP_ABSENCE_TIMEOUT_SECONDS", 0.12)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr(module, "_cleanup_resource_identity", identity)
    monkeypatch.setattr(
        module, "_wait_docker_absent", lambda kind, identifier: (kind, identifier) == ("network", resource_id)
    )
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert (
        module._cleanup_network(
            "private",
            session_digest=_plan().session_digest,
            role="browser-internal",
        )
        is True
    )
    assert len(identity_calls) == 2
    assert all(0 < timeout <= 0.12 for _, _, timeout in identity_calls)
    assert mutations == [["docker", "network", "rm", resource_id]]


def test_absence_probe_caps_each_command_to_remaining_deadline(monkeypatch) -> None:
    module = _live_module()
    clock = [100.0]
    timeouts: list[float] = []

    def fake_run(_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return module.subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(module, "CLEANUP_ABSENCE_TIMEOUT_SECONDS", 0.12)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module._wait_docker_absent("container", "a" * 64) is False
    assert timeouts
    assert all(0 < timeout <= 0.12 for timeout in timeouts)


def test_live_cleanup_quiesces_drivers_and_skips_unattempted_names(monkeypatch) -> None:
    module = _live_module()
    plan = _plan()
    events: list[str] = []

    class Driver:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> bool:
            events.append(self.name)
            return True

    def cleanup_container(name: str, **_kwargs) -> bool:
        assert events[:2] == ["browser_driver", "broker_driver"]
        events.append("browser_container" if name == plan.browser_container else "unexpected_container")
        return True

    def cleanup_network(name: str, **_kwargs) -> bool:
        assert events[:2] == ["browser_driver", "broker_driver"]
        events.append("internal_network" if name == plan.internal_network else "unexpected_network")
        return True

    monkeypatch.setattr(module, "_cleanup_container", cleanup_container)
    monkeypatch.setattr(module, "_cleanup_network", cleanup_network)
    assert (
        module._cleanup_live_resources(
            plan,
            browser_process=Driver("browser_driver"),
            broker_process=Driver("broker_driver"),
            attempted_resources=frozenset({"browser_container", "internal_network"}),
        )
        == ()
    )
    assert events == [
        "browser_driver",
        "broker_driver",
        "browser_container",
        "internal_network",
    ]


def test_run_live_session_finally_cleans_only_attempted_resources(monkeypatch) -> None:
    module = _live_module()
    digest = "sha256:" + "a" * 64
    browser_tag = "browser-run-tag"
    broker_tag = "broker-run-tag"
    now_ms = int(module.time.time() * 1000)
    build = {
        "browser_tag": browser_tag,
        "broker_tag": broker_tag,
        "platform": "linux/amd64",
        "browser_repository": "ghcr.io/seabass-up/algo-cli-boron-browser",
        "broker_repository": "ghcr.io/seabass-up/algo-cli-xenon-broker",
        "browser_index_digest": "sha256:" + "1" * 64,
        "broker_index_digest": "sha256:" + "2" * 64,
        "browser_config_digest": "sha256:" + "3" * 64,
        "broker_config_digest": "sha256:" + "4" * 64,
        "broker_code_digest": "sha256:" + "5" * 64,
        "browser_security_source": "google_version_history",
        "browser_security_latest_version": module.CHROME_VERSION,
        "browser_security_latest_release_at_ms": module.CHROME_RELEASE_AT_MS,
        "browser_security_evidence_observed_at_ms": now_ms,
        "browser_security_source_digest": digest,
    }
    cleaned: list[tuple[str, str, str]] = []

    monkeypatch.setattr(module, "_validated_build_evidence", lambda _value: build)
    monkeypatch.setattr(module, "hosted_registry_tags", lambda _environment: (browser_tag, broker_tag))
    monkeypatch.setattr(module, "_assert_native_amd64_docker", lambda: "linux/amd64")
    monkeypatch.setattr(
        module,
        "_registry_reference",
        lambda *, repository, index_digest, config_digest: repository + "@" + index_digest,
    )
    seccomp_descriptor = module.os.open(module.SECCOMP, module.os.O_RDONLY)
    monkeypatch.setattr(
        module,
        "_sealed_seccomp_profile",
        lambda _payload: (seccomp_descriptor, module.SECCOMP),
    )

    def fail_internal_network(_args, *, stage: str, timeout: int = 60) -> str:
        assert stage == "internal_network_create"
        assert timeout == 60
        raise module.LiveSessionRejected("internal_network_create_failed")

    def cleanup_network(name: str, *, session_digest: str, role: str) -> bool:
        cleaned.append((name, session_digest, role))
        return False

    monkeypatch.setattr(module, "_run", fail_internal_network)
    monkeypatch.setattr(module, "_cleanup_network", cleanup_network)
    monkeypatch.setattr(
        module,
        "_cleanup_container",
        lambda *_args, **_kwargs: pytest.fail("unattempted container cleanup"),
    )

    with pytest.raises(
        module.LiveSessionRejected,
        match="internal_network_create_failed_and_cleanup_incomplete",
    ):
        module.run_live_session(build_evidence={}, environment={})
    assert len(cleaned) == 1
    assert cleaned[0][2] == "browser-internal"
    assert cleaned[0][1].startswith("sha256:")
    with pytest.raises(OSError):
        module.os.fstat(seccomp_descriptor)


@pytest.mark.parametrize(
    ("failures", "expected_reasons"),
    [
        ({"stdin_close"}, ("driver_stdin_close_failed",)),
        ({"selector_close"}, ("driver_selector_close_failed",)),
        ({"process_poll_initial"}, ("driver_process_poll_failed",)),
        ({"process_terminate"}, ("driver_process_terminate_failed",)),
        ({"process_wait"}, ("driver_process_wait_failed",)),
        (
            {"process_wait", "process_kill"},
            ("driver_process_wait_failed", "driver_process_kill_failed"),
        ),
        (
            {"process_wait", "process_kill_wait"},
            ("driver_process_wait_failed", "driver_process_kill_wait_failed"),
        ),
        ({"stdout_close"}, ("driver_stdout_close_failed",)),
        ({"stderr_close"}, ("driver_stderr_close_failed",)),
        ({"thread_join"}, ("driver_stderr_thread_join_failed",)),
        ({"thread_state"}, ("driver_stderr_thread_state_failed",)),
        ({"thread_alive"}, ("driver_stderr_thread_alive",)),
        ({"process_poll_final"}, ("driver_process_poll_failed",)),
    ],
)
def test_driver_close_isolates_every_teardown_boundary(
    monkeypatch,
    failures: set[str],
    expected_reasons: tuple[str, ...],
) -> None:
    module = _live_module()
    events: list[str] = []
    process = _CleanupProcess(events, failures)
    selector = _CleanupSelector(events, failures)
    stderr_thread = _CleanupThread(events, failures)
    driver = object.__new__(module._FramedProcess)
    driver.process = process
    driver.stdin = process.stdin
    assert process.stdout is not None
    driver.stdout = process.stdout
    driver.stderr = process.stderr
    driver._selector = selector
    driver._stderr_thread = stderr_thread
    driver._input_finished = False

    teardown = module._teardown_framed_resources
    observed_reasons: list[tuple[str, ...]] = []

    def observed_teardown(**kwargs):
        reasons = teardown(**kwargs)
        observed_reasons.append(reasons)
        return reasons

    monkeypatch.setattr(module, "_teardown_framed_resources", observed_teardown)

    assert driver.close() is False
    assert observed_reasons == [expected_reasons]
    assert events[-1] == "process_poll_final"
    assert "stdout_close" in events
    assert "stderr_close" in events
    assert "thread_join" in events
    assert "thread_state" in events


@pytest.mark.parametrize(
    ("boundary", "expected_reason"),
    [
        ("pipes", "browser_start_pipes"),
        ("lock_create", "browser_start_setup_failed"),
        ("event_create", "browser_start_setup_failed"),
        ("selector_create", "browser_start_setup_failed"),
        ("selector_register", "browser_start_setup_failed"),
        ("thread_create", "browser_start_setup_failed"),
        ("thread_start", "browser_start_setup_failed"),
    ],
)
def test_driver_constructor_failure_rolls_back_child_selector_and_fds(
    monkeypatch,
    boundary: str,
    expected_reason: str,
) -> None:
    module = _live_module()
    events: list[str] = []
    failures = {boundary}
    process = _CleanupProcess(events, failures, missing_stdout=boundary == "pipes")
    selector = _CleanupSelector(events, failures)
    stderr_thread = _CleanupThread(events, failures)
    real_lock = module.threading.Lock
    real_event = module.threading.Event

    def lock_factory():
        events.append("lock_create")
        if boundary == "lock_create":
            raise RuntimeError("sensitive lock detail")
        return real_lock()

    def event_factory():
        events.append("event_create")
        if boundary == "event_create":
            raise RuntimeError("sensitive event detail")
        return real_event()

    def selector_factory():
        events.append("selector_create")
        if boundary == "selector_create":
            raise RuntimeError("sensitive selector detail")
        return selector

    def thread_factory(*, target, daemon):
        assert callable(target)
        assert daemon is True
        events.append("thread_create")
        if boundary == "thread_create":
            raise RuntimeError("sensitive thread detail")
        return stderr_thread

    def popen(*_args, **kwargs):
        assert kwargs["pass_fds"] == (91,)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.threading, "Lock", lock_factory)
    monkeypatch.setattr(module.threading, "Event", event_factory)
    monkeypatch.setattr(module.selectors, "DefaultSelector", selector_factory)
    monkeypatch.setattr(module.threading, "Thread", thread_factory)

    with pytest.raises(module.LiveSessionRejected) as caught:
        module._FramedProcess(["ignored"], stage="browser_start", pass_fds=(91,))
    assert caught.value.reason_code == expected_reason
    assert process.stopped is True
    assert process.stdin.closed is True
    assert process.stderr.closed is True
    if process.stdout is not None:
        assert process.stdout.closed is True
    if boundary in {"selector_register", "thread_create", "thread_start"}:
        assert selector.closed is True
    assert events[-1] == "process_poll_final"
    assert "sensitive" not in caught.value.reason_code


def test_hosted_live_session_requires_captured_seccomp_before_docker_probe(
    monkeypatch,
) -> None:
    module = _live_module()
    monkeypatch.setattr(
        module,
        "_validated_build_evidence",
        lambda _value: {"browser_tag": "browser", "broker_tag": "broker"},
    )
    monkeypatch.setattr(module, "hosted_registry_tags", lambda _environment: ("browser", "broker"))
    monkeypatch.setattr(
        module,
        "_assert_native_amd64_docker",
        lambda: pytest.fail("Docker probe ran without captured seccomp"),
    )

    with pytest.raises(module.LiveSessionRejected, match="live_seccomp_profile_required"):
        module.run_live_session(
            build_evidence={},
            environment={"GITHUB_ACTIONS": "true"},
        )


def test_sealed_seccomp_memfd_is_exact_and_immutable() -> None:
    module = _live_module()
    if not hasattr(module.os, "memfd_create"):
        pytest.skip("sealed memfd is a hosted Linux boundary")
    payload = b'{"defaultAction":"SCMP_ACT_ERRNO"}\n'
    descriptor, path = module._sealed_seccomp_profile(payload)
    try:
        assert path == Path(f"/proc/self/fd/{descriptor}")
        module.os.lseek(descriptor, 0, module.os.SEEK_SET)
        assert module.os.read(descriptor, len(payload) + 1) == payload
        module.os.lseek(descriptor, 0, module.os.SEEK_SET)
        with pytest.raises(OSError):
            module.os.write(descriptor, b"hostile")
    finally:
        module.os.close(descriptor)


def test_cleanup_failure_reason_never_uses_untyped_exception_text() -> None:
    module = _live_module()
    assert module._cleanup_failure_reason(RuntimeError("private_token")) == "live_failure_and_cleanup_incomplete"
    assert (
        module._cleanup_failure_reason(module.LiveSessionRejected("browser_result_timeout"))
        == "browser_result_timeout_and_cleanup_incomplete"
    )


def test_live_error_reporting_never_echoes_untrusted_reason_text() -> None:
    module = _live_module()
    assert module.LiveSessionRejected("private_token_value").reason_code == "live_internal_error"
    assert module._browser_terminal_failure_reason("private_token_value") == "browser_terminal_rejected"
    assert module._browser_terminal_failure_reason("navigation_failed") == "browser_navigation_failed"
    assert module._reported_failure_reason(module.BuildRejected("private_token_value")) == "browser_build_rejected"
    assert "private_token_value" not in module._reported_failure_reason(module.BoronPipeRejected("private_token_value"))
