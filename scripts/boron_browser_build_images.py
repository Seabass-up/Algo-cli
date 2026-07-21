#!/usr/bin/env python3
"""Build and attest the frozen Boron/Xenon M5 Linux images locally."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import time
from typing import Any, Iterable
import urllib.error
import urllib.request

from algo_cli.boron_browser_isolation import (
    BORON_MAX_SECURITY_LAG_MS,
    BoronBrowserFamily,
    BoronBrowserReleaseEvidence,
    BoronImagePin,
    BoronImagePurpose,
    BoronIsolationRejected,
    BoronReleaseEvidenceSource,
)


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / "algo_cli" / "resources" / "boron_browser"
PLATFORM = "linux/amd64"
CHROME_VERSION = "150.0.7871.128"
CHROME_RELEASE_AT_MS = 1_784_235_227_785
NATIVE_CHROMIUM_VERSION = "150.0.7871.124"
NATIVE_CHROMIUM_RELEASE_AT_MS = 1_784_186_325_000
BROWSER_TAG = "algo-cli/boron-browser:m5-local"
NATIVE_BROWSER_TAG = "algo-cli/carbon-browser:m5-native-local"
BROKER_TAG = "algo-cli/xenon-broker:m5-local"
VERSION_HISTORY_URL = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/"
    "stable/versions?pageSize=1&orderBy=version%20desc"
)
VERSION_RELEASE_URL_PREFIX = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/"
    "stable/versions/"
)
MAX_RELEASE_RESPONSE_BYTES = 64 * 1024
RELEASE_FETCH_TIMEOUT_SECONDS = 10
_VERSION_RE = re.compile(r"^[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}$")
_RELEASE_TIME_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})\."
    r"(?P<fraction>[0-9]{1,6})Z$"
)

BROWSER_CODE = (
    ROOT / "algo_cli" / "__init__.py",
    ROOT / "algo_cli" / "boron_browser_wrapper.py",
    ROOT / "algo_cli" / "boron_browser_entry.py",
    RESOURCE / "boron_browser_wrapper.sh",
    RESOURCE / "boron_managed_policy.json",
)
BROKER_CODE = (
    ROOT / "algo_cli" / "__init__.py",
    ROOT / "algo_cli" / "xenon_browser_egress.py",
    ROOT / "algo_cli" / "xenon_browser_broker.py",
    ROOT / "algo_cli" / "xenon_browser_entry.py",
    RESOURCE / "xenon_egress_broker.sh",
)


class BuildRejected(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_json_float(_value: str) -> None:
    raise BuildRejected("release_evidence_number")


def _reject_json_constant(_value: str) -> None:
    raise BuildRejected("release_evidence_number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise BuildRejected("release_evidence_duplicate_key")
        row[key] = value
    return row


def _strict_json(payload: bytes) -> Any:
    if not payload or len(payload) > MAX_RELEASE_RESPONSE_BYTES:
        raise BuildRejected("release_evidence_size")
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildRejected("release_evidence_json") from error


def _exact_keys(row: Any, expected: set[str], reason_code: str) -> dict[str, Any]:
    if type(row) is not dict or set(row) != expected:
        raise BuildRejected(reason_code)
    return row


def _fetch_json_bytes(url: str) -> bytes:
    release_url_re = re.compile(
        re.escape(VERSION_RELEASE_URL_PREFIX)
        + r"[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}/releases\?pageSize=1"
    )
    if url != VERSION_HISTORY_URL and not release_url_re.fullmatch(url):
        raise BuildRejected("release_evidence_url")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "algo-cli-boron-hardening/1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(request, timeout=RELEASE_FETCH_TIMEOUT_SECONDS) as response:
            if response.geturl() != url or response.status != 200:
                raise BuildRejected("release_evidence_response")
            if response.headers.get_content_type() != "application/json":
                raise BuildRejected("release_evidence_content_type")
            content_encoding = response.headers.get("Content-Encoding", "identity")
            if content_encoding.casefold() != "identity":
                raise BuildRejected("release_evidence_encoding")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise BuildRejected("release_evidence_size") from error
                if declared_length < 1 or declared_length > MAX_RELEASE_RESPONSE_BYTES:
                    raise BuildRejected("release_evidence_size")
            payload = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except BuildRejected:
        raise
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise BuildRejected("release_evidence_unavailable") from error
    if len(payload) > MAX_RELEASE_RESPONSE_BYTES:
        raise BuildRejected("release_evidence_size")
    return payload


def _parse_latest_version(payload: bytes) -> str:
    document = _exact_keys(
        _strict_json(payload),
        {"versions", "nextPageToken"},
        "release_evidence_version_shape",
    )
    versions = document["versions"]
    if type(versions) is not list or len(versions) != 1 or type(document["nextPageToken"]) is not str:
        raise BuildRejected("release_evidence_version_shape")
    row = _exact_keys(
        versions[0],
        {"name", "version"},
        "release_evidence_version_shape",
    )
    version = row["version"]
    if type(version) is not str or not _VERSION_RE.fullmatch(version):
        raise BuildRejected("release_evidence_version")
    if row["name"] != (
        "chrome/platforms/linux/channels/stable/versions/" + version
    ):
        raise BuildRejected("release_evidence_version_identity")
    return version


def _release_time_ms(value: Any) -> int:
    if type(value) is not str:
        raise BuildRejected("release_evidence_time")
    match = _RELEASE_TIME_RE.fullmatch(value)
    if match is None:
        raise BuildRejected("release_evidence_time")
    try:
        observed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BuildRejected("release_evidence_time") from error
    if observed.tzinfo != timezone.utc:
        raise BuildRejected("release_evidence_time")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed - epoch
    if delta.days < 0:
        raise BuildRejected("release_evidence_time")
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _parse_latest_release(payload: bytes, *, version: str) -> int:
    document = _exact_keys(
        _strict_json(payload),
        {"releases", "nextPageToken"},
        "release_evidence_release_shape",
    )
    releases = document["releases"]
    if type(releases) is not list or len(releases) != 1 or document["nextPageToken"] != "":
        raise BuildRejected("release_evidence_release_shape")
    row = _exact_keys(
        releases[0],
        {
            "name",
            "serving",
            "fraction",
            "version",
            "fractionGroup",
            "pinnable",
            "rolloutData",
        },
        "release_evidence_release_shape",
    )
    serving = _exact_keys(
        row["serving"],
        {"startTime"},
        "release_evidence_release_shape",
    )
    if (
        row["version"] != version
        or type(row["fraction"]) is not int
        or row["fraction"] != 1
        or row["fractionGroup"] != "1"
        or row["pinnable"] is not True
        or row["rolloutData"] != []
    ):
        raise BuildRejected("release_evidence_release_state")
    release_at_ms = _release_time_ms(serving["startTime"])
    expected_name = (
        "chrome/platforms/linux/channels/stable/versions/"
        + version
        + "/releases/"
        + str(release_at_ms // 1000)
    )
    if row["name"] != expected_name:
        raise BuildRejected("release_evidence_release_identity")
    return release_at_ms


def _release_source_digest(*, version: str, release_at_ms: int) -> str:
    canonical = json.dumps(
        {
            "release_at_ms": release_at_ms,
            "release_url": (
                VERSION_RELEASE_URL_PREFIX + version + "/releases?pageSize=1"
            ),
            "source": BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY.value,
            "version": version,
            "version_url": VERSION_HISTORY_URL,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def fetch_browser_release_evidence(
    *, observed_at_ms: int | None = None
) -> BoronBrowserReleaseEvidence:
    if observed_at_ms is not None and (
        type(observed_at_ms) is not int or observed_at_ms < 1
    ):
        raise BuildRejected("browser_security_evidence_observed_at_ms")
    version_payload = _fetch_json_bytes(VERSION_HISTORY_URL)
    version = _parse_latest_version(version_payload)
    release_url = VERSION_RELEASE_URL_PREFIX + version + "/releases?pageSize=1"
    release_payload = _fetch_json_bytes(release_url)
    release_at_ms = _parse_latest_release(release_payload, version=version)
    observed = int(time.time() * 1000) if observed_at_ms is None else observed_at_ms
    try:
        return BoronBrowserReleaseEvidence(
            source=BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY,
            browser_family=BoronBrowserFamily.CHROME_STABLE,
            browser_version=version,
            platform=PLATFORM,
            security_release_at_ms=release_at_ms,
            observed_at_ms=observed,
            source_digest=_release_source_digest(
                version=version,
                release_at_ms=release_at_ms,
            ),
        )
    except BoronIsolationRejected as error:
        raise BuildRejected(error.reason_code) from error


def _tree_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    observed = tuple(paths)
    if not observed:
        raise BuildRejected("empty_code_set")
    for path in sorted(observed, key=lambda item: item.relative_to(ROOT).as_posix()):
        if not path.is_file() or path.is_symlink():
            raise BuildRejected("code_file_missing")
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _run(
    args: list[str], *, stage: str = "command", timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildRejected(stage + "_unavailable") from error
    if result.returncode != 0:
        raise BuildRejected(stage + "_failed")
    return result


def _inspect(tag: str) -> dict[str, Any]:
    try:
        rows = json.loads(
            _run(
                ["docker", "image", "inspect", tag],
                stage="image_inspect",
                timeout=30,
            ).stdout
        )
    except json.JSONDecodeError as error:
        raise BuildRejected("inspect_json") from error
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise BuildRejected("inspect_shape")
    return rows[0]


def _labels(row: dict[str, Any]) -> dict[str, str]:
    config = row.get("Config")
    if type(config) is not dict or type(config.get("Labels")) is not dict:
        raise BuildRejected("image_labels")
    labels = config["Labels"]
    if any(type(key) is not str or type(value) is not str for key, value in labels.items()):
        raise BuildRejected("image_labels")
    return labels


def build_images(
    *,
    now_ms: int | None = None,
    release_evidence: BoronBrowserReleaseEvidence | None = None,
    include_unverified_native_browser: bool = False,
) -> dict[str, Any]:
    if now_ms is not None and (type(now_ms) is not int or now_ms < 1):
        raise BuildRejected("now_ms")
    if type(include_unverified_native_browser) is not bool:
        raise BuildRejected("native_browser_option")
    authoritative_release = (
        fetch_browser_release_evidence()
        if release_evidence is None
        else release_evidence
    )
    observed_now = int(time.time() * 1000) if now_ms is None else now_ms
    browser_pin = BoronImagePin(
        "algo-cli/boron-browser@sha256:" + "0" * 64,
        BoronImagePurpose.PUBLIC_MANAGED,
        BoronBrowserFamily.CHROME_STABLE,
        CHROME_VERSION,
        PLATFORM,
        CHROME_RELEASE_AT_MS,
    )
    try:
        update_lag_ms = browser_pin.security_update_lag_ms(
            now_ms=observed_now,
            release_evidence=authoritative_release,
        )
    except BoronIsolationRejected as error:
        raise BuildRejected(error.reason_code) from error
    _run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        stage="docker_info",
        timeout=30,
    )

    browser_code_digest = _tree_digest(BROWSER_CODE)
    broker_code_digest = _tree_digest(BROKER_CODE)
    builds = [
        (
            RESOURCE / "boron_public_browser.Dockerfile",
            BROWSER_TAG,
            "BORON_CODE_DIGEST=" + browser_code_digest,
            "browser_build",
            PLATFORM,
        ),
        (
            RESOURCE / "xenon_egress_broker.Dockerfile",
            BROKER_TAG,
            "XENON_CODE_DIGEST=" + broker_code_digest,
            "broker_build",
            PLATFORM,
        ),
    ]
    if include_unverified_native_browser:
        builds.insert(
            1,
            (
                RESOURCE / "carbon_native_browser.Dockerfile",
                NATIVE_BROWSER_TAG,
                "BORON_CODE_DIGEST=" + browser_code_digest,
                "native_browser_build",
                "linux/arm64",
            ),
        )
    for dockerfile, tag, build_arg, stage, build_platform in builds:
        _run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                build_platform,
                "--load",
                "--provenance=false",
                "--file",
                str(dockerfile),
                "--build-arg",
                build_arg,
                "--tag",
                tag,
                ".",
            ],
            stage=stage,
        )

    browser = _inspect(BROWSER_TAG)
    broker = _inspect(BROKER_TAG)
    browser_labels = _labels(browser)
    broker_labels = _labels(broker)
    if (
        browser_labels.get("com.algo-cli.role") != "managed-browser"
        or browser_labels.get("com.algo-cli.code.sha256") != browser_code_digest
        or browser_labels.get("com.algo-cli.browser.version") != CHROME_VERSION
        or browser_labels.get("com.algo-cli.browser.release-at-ms")
        != str(CHROME_RELEASE_AT_MS)
        or broker_labels.get("com.algo-cli.role") != "egress-broker"
        or broker_labels.get("com.algo-cli.code.sha256") != broker_code_digest
        or browser.get("Architecture") != "amd64"
        or broker.get("Architecture") != "amd64"
        or browser.get("Config", {}).get("User") != "1000:1000"
        or broker.get("Config", {}).get("User") != "1001:1001"
    ):
        raise BuildRejected("image_identity_mismatch")

    native_browser: dict[str, Any] | None = None
    if include_unverified_native_browser:
        native_browser = _inspect(NATIVE_BROWSER_TAG)
        native_labels = _labels(native_browser)
        if (
            native_labels.get("com.algo-cli.role") != "managed-browser"
            or native_labels.get("com.algo-cli.code.sha256") != browser_code_digest
            or native_labels.get("com.algo-cli.browser.family") != "chromium_stable"
            or native_labels.get("com.algo-cli.browser.version")
            != NATIVE_CHROMIUM_VERSION
            or native_labels.get("com.algo-cli.browser.release-at-ms")
            != str(NATIVE_CHROMIUM_RELEASE_AT_MS)
            or native_browser.get("Architecture") != "arm64"
            or native_browser.get("Config", {}).get("User") != "1000:1000"
        ):
            raise BuildRejected("native_image_identity_mismatch")

    chrome_version = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            PLATFORM,
            "--entrypoint",
            "/opt/google/chrome/chrome",
            BROWSER_TAG,
            "--version",
        ],
        stage="browser_version_probe",
        timeout=60,
    ).stdout.strip()
    if chrome_version != "Google Chrome " + CHROME_VERSION:
        raise BuildRejected("browser_version_mismatch")
    module_probes = [
        (BROWSER_TAG, "algo_cli.boron_browser_entry"),
        (BROKER_TAG, "algo_cli.xenon_browser_entry"),
    ]
    if include_unverified_native_browser:
        native_version = _run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/arm64",
                "--entrypoint",
                "/usr/bin/chromium",
                NATIVE_BROWSER_TAG,
                "--version",
            ],
            stage="native_browser_version_probe",
            timeout=60,
        ).stdout.strip()
        if not native_version.startswith("Chromium " + NATIVE_CHROMIUM_VERSION):
            raise BuildRejected("native_browser_version_mismatch")
        module_probes.insert(
            1,
            (NATIVE_BROWSER_TAG, "algo_cli.boron_browser_entry"),
        )
    for tag, module in module_probes:
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/arm64" if tag == NATIVE_BROWSER_TAG else PLATFORM,
                "--entrypoint",
                "/usr/bin/python3",
                tag,
                "-B",
                "-I",
                "-c",
                f"import cryptography; import {module}; assert cryptography.__version__ == '49.0.0'",
            ],
            stage="module_import_probe",
            timeout=60,
        )

    if release_evidence is None:
        authoritative_release = fetch_browser_release_evidence()
        observed_now = int(time.time() * 1000)
        try:
            update_lag_ms = browser_pin.security_update_lag_ms(
                now_ms=observed_now,
                release_evidence=authoritative_release,
            )
        except BoronIsolationRejected as error:
            raise BuildRejected(error.reason_code) from error

    evidence = {
        "schema_version": 1,
        "platform": PLATFORM,
        "browser_tag": BROWSER_TAG,
        "browser_image_id": browser.get("Id"),
        "browser_code_digest": browser_code_digest,
        "browser_version": CHROME_VERSION,
        "browser_security_update_lag_ms": update_lag_ms,
        "browser_security_max_update_lag_ms": BORON_MAX_SECURITY_LAG_MS,
        "browser_security_latest_version": authoritative_release.browser_version,
        "browser_security_latest_release_at_ms": (
            authoritative_release.security_release_at_ms
        ),
        "browser_security_evidence_observed_at_ms": (
            authoritative_release.observed_at_ms
        ),
        "browser_security_source": authoritative_release.source.value,
        "browser_security_source_digest": authoritative_release.source_digest,
        "native_browser_built": include_unverified_native_browser,
        "native_browser_fresh": False,
        "native_browser_freshness_reason": "upstream_patch_equivalence_unverified",
        "broker_tag": BROKER_TAG,
        "broker_image_id": broker.get("Id"),
        "broker_code_digest": broker_code_digest,
        "cryptography_version": "49.0.0",
        "non_root_defaults": True,
    }
    if native_browser is not None:
        evidence.update(
            {
                "native_browser_tag": NATIVE_BROWSER_TAG,
                "native_browser_image_id": native_browser.get("Id"),
                "native_browser_version": NATIVE_CHROMIUM_VERSION,
                "native_browser_security_update_lag_ms": None,
            }
        )
    if any(
        key.endswith("_image_id")
        and (type(value) is not str or not value.startswith("sha256:"))
        for key, value in evidence.items()
    ):
        raise BuildRejected("image_id")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-unverified-native-browser",
        action="store_true",
        help="also build the arm64 Chromium image, which is not public-route eligible",
    )
    args = parser.parse_args(argv)
    try:
        evidence = build_images(
            include_unverified_native_browser=args.include_unverified_native_browser
        )
    except BuildRejected as error:
        print(json.dumps({"status": "failed", "reason_code": str(error)}, sort_keys=True))
        return 1
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("ascii")
    print(
        json.dumps(
            {
                "status": "passed",
                "evidence": evidence,
                "evidence_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                "limitation": "Local image build and identity proof; no registry provenance or browser session claim.",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
