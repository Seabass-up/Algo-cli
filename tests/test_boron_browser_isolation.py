from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
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
        tmpfs = {
            "/tmp": "rw,noexec,nosuid,nodev,mode=0700,uid=1001,gid=1001,size=67108864"
        }
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
                "Networks": {
                    plan.internal_network: {"IPAddress": plan.browser_internal_ip}
                },
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
    assert ("--cap-drop", "ALL") == argv[
        argv.index("--cap-drop") : argv.index("--cap-drop") + 2
    ]
    assert "no-new-privileges=true" in argv
    assert f"seccomp={seccomp}" in argv
    assert "--publish" not in argv and "-p" not in argv
    assert "--volume" not in argv and "-v" not in argv and "--mount" not in argv
    assert "--privileged" not in argv
    assert "--network" in argv and _plan().internal_network in argv
    assert ("--ip", _plan().browser_internal_ip) == argv[
        argv.index("--ip") : argv.index("--ip") + 2
    ]
    assert "NO_PROXY=" in argv and "ALL_PROXY=" in argv
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
    assert ("--subnet", _plan().internal_subnet) == argv[
        argv.index("--subnet") : argv.index("--subnet") + 2
    ]
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
    assert ("--network", _plan().internal_network) == argv[
        argv.index("--network") : argv.index("--network") + 2
    ]
    assert ("--ip", _plan().broker_internal_ip) == argv[
        argv.index("--ip") : argv.index("--ip") + 2
    ]
    assert "--read-only" in argv
    assert ("--cap-drop", "ALL") == argv[
        argv.index("--cap-drop") : argv.index("--cap-drop") + 2
    ]
    assert "no-new-privileges=true" in argv
    assert "--publish" not in argv and "--volume" not in argv and "--mount" not in argv
    assert ":latest" not in rendered
    assert _broker_image().reference in argv
    assert _broker_image().binary_digest in rendered
    assert argv[-1] == "/opt/algo/bin/xenon-egress-broker"
    assert "--internal" not in launch.create_egress_network_argv()
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
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Memory", BORON_MAX_BROWSER_MEMORY_BYTES + 1), "memory_limit_evidence"),
        (lambda n, b, r: b[0]["Config"].__setitem__("User", "0:0"), "root_user"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("AutoRemove", False), "auto_remove_missing"),
        (lambda n, b, r: b[0]["HostConfig"].__setitem__("Devices", [{"PathOnHost": "/dev/x"}]), "device_exposure"),
        (lambda n, b, r: b[0]["NetworkSettings"].__setitem__("Networks", {_plan().internal_network: {}, "bridge": {}}), "browser_network_bypass"),
        (lambda n, b, r: r[0]["NetworkSettings"].__setitem__("Networks", {_plan().internal_network: {}}), "broker_network_topology"),
        (lambda n, b, r: b[0]["Config"].__setitem__("Image", "other@sha256:" + "9" * 64), "image_identity_mismatch"),
        (lambda n, b, r: b[0].__setitem__("Path", "/bin/sh"), "browser_command_evidence"),
        (lambda n, b, r: r[0].__setitem__("Path", "/bin/sh"), "broker_image_identity_mismatch"),
        (lambda n, b, r: r[0]["Config"].__setitem__("Image", "other@sha256:" + "9" * 64), "broker_image_identity_mismatch"),
        (lambda n, b, r: b[0]["Config"].__setitem__("Env", ["HTTP_PROXY=http://evil"]), "browser_proxy_environment"),
        (lambda n, b, r: r[0]["Config"].__setitem__("Env", ["XENON_LISTEN_ADDRESS=0.0.0.0", "XENON_LISTEN_PORT=3128"]), "broker_environment"),
        (lambda n, b, r: r[0]["HostConfig"].__setitem__("NetworkMode", "bridge"), "broker_network_mode"),
        (lambda n, b, r: r[0].__setitem__("Mounts", [{"Type": "tmpfs", "Destination": "/tmp"}, {"Type": "tmpfs", "Destination": "/algo-profile"}]), "host_mount"),
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
    assert (
        probe_docker_image(image, runner=daemon)
        is BoronReadinessState.DOCKER_DAEMON_UNAVAILABLE
    )

    security = Runner([(0, json.dumps({"Os": "linux"})), (0, json.dumps(["name=cgroupns"]))])
    assert (
        probe_docker_image(image, runner=security)
        is BoronReadinessState.DOCKER_SECURITY_UNAVAILABLE
    )

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
    assert (
        probe_docker_image(image, runner=mismatch)
        is BoronReadinessState.IMAGE_IDENTITY_MISMATCH
    )


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
    assert {"chrome://*", "chrome-untrusted://*", "devtools://*", "file://*"} <= set(
        policy["URLBlocklist"]
    )


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
