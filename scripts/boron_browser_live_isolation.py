#!/usr/bin/env python3
"""Exercise Docker's browser-container boundary without enabling browser use."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SECCOMP = ROOT / "algo_cli" / "resources" / "boron_browser" / "boron_seccomp_profile.json"
DEFAULT_PROBE_IMAGE = (
    "sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28"
)
_IMAGE_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._/-]{0,254}@)?sha256:[0-9a-f]{64}$")

_PROBE_CODE = r"""
import json
import socket
import time

def blocked(host, port):
    try:
        connection = socket.create_connection((host, port), timeout=2.0)
    except OSError:
        return True
    connection.close()
    return False

print(json.dumps({
    "public_ip_blocked": blocked("1.1.1.1", 443),
    "metadata_blocked": blocked("169.254.169.254", 80),
    "host_alias_blocked": blocked("host.docker.internal", 80),
}, sort_keys=True), flush=True)
time.sleep(20)
""".strip()


class ProbeError(RuntimeError):
    pass


def _run(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("command_unavailable") from exc
    if result.returncode != 0:
        raise ProbeError("command_failed")
    return result


def _one_json_list(text: str, label: str) -> dict[str, Any]:
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{label}_json") from exc
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise ProbeError(f"{label}_shape")
    return rows[0]


def run_probe(*, image: str, platform: str) -> dict[str, Any]:
    if type(image) is not str or not _IMAGE_RE.fullmatch(image):
        raise ProbeError("image_digest_required")
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ProbeError("platform")
    if not SECCOMP.is_file():
        raise ProbeError("seccomp_missing")

    suffix = secrets.token_hex(4)
    network_name = f"boron-live-{suffix}"
    container_name = f"boron-live-{suffix}"
    cleanup: dict[str, bool] = {"container": False, "network": False}
    created_network = False
    started_container = False
    try:
        _run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--label",
                "com.algo-cli.role=browser-live-probe",
                network_name,
            ]
        )
        created_network = True
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--network",
                network_name,
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--security-opt",
                f"seccomp={SECCOMP}",
                "--pids-limit",
                "32",
                "--memory",
                "268435456",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,mode=0700,uid=65534,gid=65534,size=16777216",
                "--platform",
                platform,
                image,
                "python",
                "-c",
                _PROBE_CODE,
            ],
            timeout=60,
        )
        started_container = True
        inspect = _one_json_list(
            _run(["docker", "inspect", container_name]).stdout,
            "container",
        )
        network = _one_json_list(
            _run(["docker", "network", "inspect", network_name]).stdout,
            "network",
        )
        logs: list[str] = []
        for _attempt in range(30):
            logs = _run(["docker", "logs", container_name]).stdout.splitlines()
            if logs:
                break
            time.sleep(0.1)
        if not logs:
            raise ProbeError("probe_output_missing")
        try:
            attempts = json.loads(logs[0])
        except json.JSONDecodeError as exc:
            raise ProbeError("probe_output_json") from exc
        host = inspect.get("HostConfig")
        config = inspect.get("Config")
        network_settings = inspect.get("NetworkSettings")
        if type(host) is not dict or type(config) is not dict or type(network_settings) is not dict:
            raise ProbeError("inspect_shape")
        networks = network_settings.get("Networks")
        ports = network_settings.get("Ports")
        members = network.get("Containers")
        security = host.get("SecurityOpt")
        evidence = {
            "schema_version": 1,
            "public_ip_blocked": attempts.get("public_ip_blocked") is True,
            "metadata_blocked": attempts.get("metadata_blocked") is True,
            "host_alias_blocked": attempts.get("host_alias_blocked") is True,
            "network_internal": network.get("Internal") is True,
            "network_driver_bridge": network.get("Driver") == "bridge",
            "single_network": type(networks) is dict and set(networks) == {network_name},
            "single_participant": type(members) is dict and len(members) == 1,
            "read_only_root": host.get("ReadonlyRootfs") is True,
            "non_root": config.get("User") == "65534:65534",
            "capabilities_dropped": host.get("CapDrop") == ["ALL"],
            "no_new_privileges": type(security) is list
            and "no-new-privileges=true" in security,
            "custom_seccomp": type(security) is list
            and any(type(item) is str and item.startswith("seccomp=") for item in security),
            "no_published_ports": ports == {} and host.get("PublishAllPorts") is False,
            "no_host_binds": host.get("Binds") in (None, []),
            "private_ipc": host.get("IpcMode") == "private",
            "private_pid_namespace": host.get("PidMode") in ("", "private"),
            "pids_limited": host.get("PidsLimit") == 32,
            "memory_limited": host.get("Memory") == 268435456,
            "image_digest_pinned": image.startswith("sha256:") or "@sha256:" in image,
            "platform": platform,
        }
        failed = [key for key, value in evidence.items() if key not in {"schema_version", "platform"} and value is not True]
        if failed:
            raise ProbeError("invariant_failed:" + ",".join(sorted(failed)))
        return evidence
    finally:
        if started_container:
            result = subprocess.run(
                ["docker", "stop", "--time", "1", container_name],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            cleanup["container"] = result.returncode == 0
        if created_network:
            result = subprocess.run(
                ["docker", "network", "rm", network_name],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            cleanup["network"] = result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_PROBE_IMAGE)
    parser.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args(argv)
    try:
        evidence = run_probe(image=args.image, platform=args.platform)
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "reason_code": str(exc)}, sort_keys=True))
        return 1
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("ascii")
    result = {
        "status": "passed",
        "evidence": evidence,
        "evidence_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "limitation": "Docker boundary probe only; no browser or egress broker was started.",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
