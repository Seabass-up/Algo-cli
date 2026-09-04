#!/usr/bin/env python3
"""Build and attest the frozen Boron/Xenon M5 Linux images locally."""

from __future__ import annotations

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Iterable, Mapping
import urllib.error
import urllib.parse
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
CHROME_VERSION = "151.0.7922.108"
CHROME_DEBIAN_VERSION = CHROME_VERSION + "-1"
CHROME_RELEASE_AT_MS = 1_786_046_667_459
CRYPTOGRAPHY_VERSION = "50.0.0"
CFFI_VERSION = "2.1.0"
PYCPARSER_VERSION = "3.0"
NATIVE_CHROMIUM_VERSION = "150.0.7871.124"
NATIVE_CHROMIUM_RELEASE_AT_MS = 1_784_186_325_000
BROWSER_TAG = "algo-cli/boron-browser:m5-local"
NATIVE_BROWSER_TAG = "algo-cli/carbon-browser:m5-native-local"
BROKER_TAG = "algo-cli/xenon-broker:m5-local"
VERSION_HISTORY_URL = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/"
    "stable/versions?pageSize=1&orderBy=version%20desc"
)
VERSION_RELEASE_URL_PREFIX = "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions/"
MAX_RELEASE_RESPONSE_BYTES = 64 * 1024
MAX_BUILD_METADATA_BYTES = 2 * 1024 * 1024
MAX_REGISTRY_ATTESTATION_BYTES = 16 * 1024 * 1024
MAX_DOCKER_CONFIG_BYTES = 128 * 1024
MAX_BUILD_CONTEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_BUILD_CONTEXT_BYTES = 64 * 1024 * 1024
RELEASE_FETCH_TIMEOUT_SECONDS = 10
HOSTED_REPOSITORY = "Seabass-up/Algo-cli"
HOSTED_REPOSITORY_ID = "1297752684"
_VERSION_RE = re.compile(r"^[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_INTEGER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_REGISTRY_RE = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]{0,38})/"
    r"[a-z0-9](?:[a-z0-9._-]{0,126})$"
)
_REGISTRY_TAG_RE = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]{0,38})/"
    r"[a-z0-9](?:[a-z0-9._-]{0,126})"
    r":run-[1-9][0-9]{0,19}-[1-9][0-9]{0,19}-[0-9a-f]{40}$"
)
_RELEASE_TIME_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})\."
    r"(?P<fraction>[0-9]{1,6})Z$"
)
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z$"
)
_HEX_DIGEST_VALUE_RE = re.compile(r"^[0-9a-f]{32,128}$")
_DIGEST_ALGORITHM_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,31}$")
_SLSA_BUILD_TYPE = "https://mobyproject.org/buildkit@v1"
_SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v0.2"
_SPDX_PREDICATE_TYPE = "https://spdx.dev/Document"
_INTOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_SPDX_VERSIONS = frozenset({"SPDX-2.2", "SPDX-2.3"})
_OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_OCI_EMPTY_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
_INTOTO_MEDIA_TYPE = "application/vnd.in-toto+json"
_ATTESTATION_ARTIFACT_TYPE = "application/vnd.docker.attestation.manifest.v1+json"
_EMPTY_JSON_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"

# Official Docker Verified Publisher descriptors, resolved from Docker Hub on
# 2026-08-09. BuildKit v0.32.2 records the reachable frontend SourceOp's
# linux/amd64 manifest digest, while its scanner metadata resolution records
# the scanner's immutable root index digest.
DOCKERFILE_FRONTEND_REFERENCE = (
    "docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32"
)
DOCKERFILE_FRONTEND_AMD64_DIGEST = "sha256:34b128e419449565adc5ed7f487a6f503a73f1077012cfed86354c731338c44f"
SBOM_GENERATOR_REFERENCE = (
    "docker/buildkit-syft-scanner:1.11.0@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
)
SBOM_GENERATOR_INDEX_DIGEST = "sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
DEBIAN_BASE_REFERENCE = "debian:bookworm-slim@sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"
DEBIAN_BASE_AMD64_DIGEST = "sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"
DEBIAN_SNAPSHOT = "20260712T202631Z"
DEBIAN_SECURITY_SNAPSHOT = "20260712T194830Z"
BROWSER_DPKG_LOCK_DIGEST = "sha256:8de022828059888145925f8fc14424eb1f8b9a2d01d5bb24abff9d2d0d60a1d9"
BROWSER_DPKG_LOCK_ENTRIES = "228"
BROKER_DPKG_LOCK_DIGEST = "sha256:945e9057beb01efbdcf89ca6ba002f260eb6bda40f5d535337e7ca7dc6eed640"
BROKER_DPKG_LOCK_ENTRIES = "122"
_PINNED_PROVENANCE_MATERIALS = {
    (
        "pkg:docker/docker/dockerfile@1.26.0?"
        "digest=sha256%3Aecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32"
        "&platform=linux%2Famd64"
    ): DOCKERFILE_FRONTEND_AMD64_DIGEST,
    (
        "pkg:docker/docker/buildkit-syft-scanner@1.11.0?"
        "digest=sha256%3A79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
        "&platform=linux%2Famd64"
    ): SBOM_GENERATOR_INDEX_DIGEST,
    (
        "pkg:docker/debian@bookworm-slim?"
        "digest=sha256%3A63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"
        "&platform=linux%2Famd64"
    ): DEBIAN_BASE_AMD64_DIGEST,
}

_PYTHON_SBOM_COMPONENTS = (
    ("cryptography", CRYPTOGRAPHY_VERSION, "pkg:pypi/cryptography@50.0.0"),
    ("cffi", CFFI_VERSION, "pkg:pypi/cffi@2.1.0"),
    ("pycparser", PYCPARSER_VERSION, "pkg:pypi/pycparser@3.0"),
)
_SBOM_COMPONENTS_BY_DOCKERFILE = {
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile": (
        (
            "google-chrome-stable",
            CHROME_DEBIAN_VERSION,
            "pkg:deb/debian/google-chrome-stable@151.0.7922.108-1?arch=amd64&distro=debian-12",
        ),
        *_PYTHON_SBOM_COMPONENTS,
    ),
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile": _PYTHON_SBOM_COMPONENTS,
}
_SBOM_FORBIDDEN_COMPONENTS_BY_DOCKERFILE = {
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile": frozenset(),
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile": frozenset({"google-chrome-stable"}),
}
_REGISTRY_REPOSITORY_BY_DOCKERFILE = {
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile": ("ghcr.io/seabass-up/algo-cli-boron-browser"),
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile": ("ghcr.io/seabass-up/algo-cli-xenon-broker"),
}

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

# Henry's qualification digest covers this exact source set. Hosted image
# builds consume a canonical in-memory archive of the same bytes, so the digest
# checked before qualification is also independently checked at the Buildx
# input boundary.
HOSTED_BUILD_CONTEXT_PATHS = (
    ".github/workflows/oliver-ci.yml",
    "pyproject.toml",
    "uv.lock",
    "algo_cli/__init__.py",
    "algo_cli/boron_browser_entry.py",
    "algo_cli/boron_browser_isolation.py",
    "algo_cli/boron_browser_wrapper.py",
    "algo_cli/xenon_browser_broker.py",
    "algo_cli/xenon_browser_egress.py",
    "algo_cli/xenon_browser_entry.py",
    "algo_cli/resources/boron_browser/boron_browser_wrapper.sh",
    "algo_cli/resources/boron_browser/boron_managed_policy.json",
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile.dockerignore",
    "algo_cli/resources/boron_browser/boron_seccomp_profile.json",
    "algo_cli/resources/boron_browser/xenon_egress_broker.sh",
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile",
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile.dockerignore",
    "scripts/boron_browser_build_images.py",
    "scripts/boron_browser_live_session.py",
    "scripts/henry_boron_hosted_qualification.py",
    "tests/test_boron_browser_entry.py",
    "tests/test_boron_browser_images.py",
    "tests/test_boron_browser_isolation.py",
    "tests/test_boron_browser_wrapper.py",
    "tests/test_henry_boron_hosted_qualification.py",
    "tests/test_xenon_browser_broker.py",
    "tests/test_xenon_browser_egress.py",
    "tests/test_xenon_browser_entry.py",
)
_HOSTED_BUILD_CONTEXT_FILES = frozenset(HOSTED_BUILD_CONTEXT_PATHS)
_HOSTED_BUILD_CONTEXT_DIRECTORIES = frozenset(
    {
        Path(relative).parent.as_posix()
        for relative in HOSTED_BUILD_CONTEXT_PATHS
        if Path(relative).parent.as_posix() != "."
    }
    | {
        ancestor.as_posix()
        for relative in HOSTED_BUILD_CONTEXT_PATHS
        for ancestor in Path(relative).parents
        if ancestor.as_posix() != "."
    }
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


def _strict_build_metadata(path: Path) -> tuple[dict[str, Any], str]:
    """Read one bounded, single-link metadata file without following replacements."""

    try:
        info = path.lstat()
    except OSError as error:
        raise BuildRejected("build_metadata_file") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or not 1 <= info.st_size <= MAX_BUILD_METADATA_BYTES
    ):
        raise BuildRejected("build_metadata_file")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size)
        ):
            raise BuildRejected("build_metadata_changed")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                raise BuildRejected("build_metadata_changed")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            raise BuildRejected("build_metadata_changed")
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
            raise BuildRejected("build_metadata_changed")
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildRejected("build_metadata_json") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if type(document) is not dict:
        raise BuildRejected("build_metadata_shape")
    return document, "sha256:" + hashlib.sha256(payload).hexdigest()


def hosted_registry_tags(environment: Mapping[str, str]) -> tuple[str, str]:
    """Derive run-unique GHCR tags only for the pinned trusted repository."""

    if (
        type(environment) is not dict
        or environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_EVENT_NAME") != "push"
        or environment.get("GITHUB_REPOSITORY") != HOSTED_REPOSITORY
        or environment.get("GITHUB_REPOSITORY_ID") != HOSTED_REPOSITORY_ID
        or environment.get("GITHUB_REF") != "refs/heads/main"
        or environment.get("GITHUB_REF_PROTECTED") != "true"
        or environment.get("GITHUB_WORKFLOW_REF")
        != HOSTED_REPOSITORY + "/.github/workflows/oliver-ci.yml@refs/heads/main"
        or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or environment.get("RUNNER_OS") != "Linux"
        or environment.get("RUNNER_ARCH") != "X64"
    ):
        raise BuildRejected("registry_environment")
    revision = environment.get("GITHUB_SHA", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    workflow_revision = environment.get("GITHUB_WORKFLOW_SHA", "")
    if (
        _REVISION_RE.fullmatch(revision) is None
        or workflow_revision != revision
        or _INTEGER_RE.fullmatch(run_id) is None
        or _INTEGER_RE.fullmatch(attempt) is None
    ):
        raise BuildRejected("registry_identity")
    prefix = "ghcr.io/seabass-up/algo-cli"
    suffix = f":run-{run_id}-{attempt}-{revision}"
    browser = prefix + "-boron-browser" + suffix
    broker = prefix + "-xenon-broker" + suffix
    if _REGISTRY_TAG_RE.fullmatch(browser) is None or _REGISTRY_TAG_RE.fullmatch(broker) is None:
        raise BuildRejected("registry_tag")
    return browser, broker


def _exact_keys(row: Any, expected: set[str], reason_code: str) -> dict[str, Any]:
    if type(row) is not dict or set(row) != expected:
        raise BuildRejected(reason_code)
    return row


def _fetch_json_bytes(url: str) -> bytes:
    release_url_re = re.compile(
        re.escape(VERSION_RELEASE_URL_PREFIX) + r"[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}/releases\?pageSize=1"
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
    if row["name"] != ("chrome/platforms/linux/channels/stable/versions/" + version):
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
        "chrome/platforms/linux/channels/stable/versions/" + version + "/releases/" + str(release_at_ms // 1000)
    )
    if row["name"] != expected_name:
        raise BuildRejected("release_evidence_release_identity")
    return release_at_ms


def _release_source_digest(*, version: str, release_at_ms: int) -> str:
    canonical = json.dumps(
        {
            "release_at_ms": release_at_ms,
            "release_url": (VERSION_RELEASE_URL_PREFIX + version + "/releases?pageSize=1"),
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


def fetch_browser_release_evidence(*, observed_at_ms: int | None = None) -> BoronBrowserReleaseEvidence:
    if observed_at_ms is not None and (type(observed_at_ms) is not int or observed_at_ms < 1):
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


def _normalized_context_payloads(
    payloads: Iterable[tuple[str, bytes]],
) -> dict[str, bytes]:
    """Validate the exact source set used by hosted qualification and builds."""

    rows: dict[str, bytes] = {}
    try:
        for relative, payload in payloads:
            if (
                type(relative) is not str
                or relative not in _HOSTED_BUILD_CONTEXT_FILES
                or relative in rows
                or type(payload) is not bytes
                or not 1 <= len(payload) <= MAX_BUILD_CONTEXT_FILE_BYTES
            ):
                raise BuildRejected("registry_build_context")
            rows[relative] = payload
    except (TypeError, ValueError) as error:
        raise BuildRejected("registry_build_context") from error
    if set(rows) != _HOSTED_BUILD_CONTEXT_FILES:
        raise BuildRejected("registry_build_context")
    return rows


def _source_payload_digest(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(HOSTED_BUILD_CONTEXT_PATHS):
        payload = payloads[relative]
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _payload_tree_digest(
    paths: Iterable[Path],
    *,
    payloads: Mapping[str, bytes],
) -> str:
    digest = hashlib.sha256()
    observed = tuple(path.relative_to(ROOT).as_posix() for path in paths)
    if not observed or any(relative not in payloads for relative in observed):
        raise BuildRejected("registry_build_context")
    for relative in sorted(observed):
        encoded = relative.encode("utf-8")
        payload = payloads[relative]
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def canonical_build_context_archive(
    payloads: Iterable[tuple[str, bytes]],
) -> bytes:
    """Create the one deterministic USTAR byte stream accepted by Buildx."""

    rows = _normalized_context_payloads(payloads)
    output = io.BytesIO()
    try:
        with tarfile.open(
            fileobj=output,
            mode="w:",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            for relative in sorted(_HOSTED_BUILD_CONTEXT_DIRECTORIES):
                member = tarfile.TarInfo(relative)
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = 0
                archive.addfile(member)
            for relative in sorted(rows):
                payload = rows[relative]
                member = tarfile.TarInfo(relative)
                member.mode = 0o644
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.mtime = 0
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    except (OSError, tarfile.TarError, ValueError) as error:
        raise BuildRejected("registry_build_context") from error
    result = output.getvalue()
    if not 1 <= len(result) <= MAX_BUILD_CONTEXT_BYTES:
        raise BuildRejected("registry_build_context")
    return result


def _validated_build_context_archive(
    context_archive: bytes,
    *,
    qualification_source_digest: str,
) -> dict[str, bytes]:
    """Parse but never extract the canonical archive consumed by Buildx."""

    if (
        type(context_archive) is not bytes
        or not 1 <= len(context_archive) <= MAX_BUILD_CONTEXT_BYTES
        or type(qualification_source_digest) is not str
        or _DIGEST_RE.fullmatch(qualification_source_digest) is None
    ):
        raise BuildRejected("registry_build_context")
    rows: dict[str, bytes] = {}
    directories: set[str] = set()
    try:
        with tarfile.open(
            fileobj=io.BytesIO(context_archive),
            mode="r:",
        ) as archive:
            count = 0
            while (member := archive.next()) is not None:
                count += 1
                if count > len(_HOSTED_BUILD_CONTEXT_FILES) + len(_HOSTED_BUILD_CONTEXT_DIRECTORIES):
                    raise BuildRejected("registry_build_context")
                name = member.name
                if (
                    type(name) is not str
                    or not name
                    or Path(name).is_absolute()
                    or Path(*Path(name).parts).as_posix() != name
                    or any(part in {"", ".", ".."} for part in Path(name).parts)
                    or member.pax_headers
                ):
                    raise BuildRejected("registry_build_context")
                if member.isdir():
                    if name not in _HOSTED_BUILD_CONTEXT_DIRECTORIES or name in directories:
                        raise BuildRejected("registry_build_context")
                    directories.add(name)
                    continue
                if not member.isfile() or name not in _HOSTED_BUILD_CONTEXT_FILES or name in rows:
                    raise BuildRejected("registry_build_context")
                source = archive.extractfile(member)
                if source is None or not 1 <= member.size <= MAX_BUILD_CONTEXT_FILE_BYTES:
                    raise BuildRejected("registry_build_context")
                payload = source.read(MAX_BUILD_CONTEXT_FILE_BYTES + 1)
                if len(payload) != member.size:
                    raise BuildRejected("registry_build_context")
                rows[name] = payload
    except (OSError, EOFError, tarfile.TarError, UnicodeError, ValueError) as error:
        raise BuildRejected("registry_build_context") from error
    if directories != _HOSTED_BUILD_CONTEXT_DIRECTORIES:
        raise BuildRejected("registry_build_context")
    normalized = _normalized_context_payloads(rows.items())
    if (
        _source_payload_digest(normalized) != qualification_source_digest
        or canonical_build_context_archive(normalized.items()) != context_archive
    ):
        raise BuildRejected("registry_build_context")
    return normalized


def _tree_digest(
    paths: Iterable[Path],
    *,
    root: Path | None = None,
    require_immutable: bool = False,
) -> str:
    digest = hashlib.sha256()
    observed = tuple(paths)
    digest_root = ROOT if root is None else root
    if not observed:
        raise BuildRejected("empty_code_set")
    for path in sorted(observed, key=lambda item: item.relative_to(digest_root).as_posix()):
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise BuildRejected("code_file_missing") from error
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or resolved != path
            or (require_immutable and (info.st_uid != os.getuid() or info.st_mode & 0o222))
        ):
            raise BuildRejected("code_file_missing")
        relative = path.relative_to(digest_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _run(
    args: list[str],
    *,
    stage: str = "command",
    timeout: int = 900,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    if environment is not None and (
        type(environment) is not dict
        or any(type(key) is not str or type(value) is not str for key, value in environment.items())
    ):
        raise BuildRejected(stage + "_environment")
    command_environment = None
    if environment is not None:
        command_environment = dict(os.environ)
        command_environment.update(environment)
    if input_bytes is not None and type(input_bytes) is not bytes:
        raise BuildRejected(stage + "_input")
    command_root = ROOT if working_directory is None else working_directory
    try:
        if input_bytes is None:
            result = subprocess.run(
                args,
                cwd=command_root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=command_environment,
            )
        else:
            raw = subprocess.run(
                args,
                cwd=command_root,
                check=False,
                capture_output=True,
                input=input_bytes,
                text=False,
                timeout=timeout,
                env=command_environment,
            )
            result = subprocess.CompletedProcess(
                raw.args,
                raw.returncode,
                raw.stdout.decode("utf-8", errors="replace"),
                raw.stderr.decode("utf-8", errors="replace"),
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


def _parse_rfc3339(value: Any, *, stage: str) -> datetime:
    if type(value) is not str or _RFC3339_RE.fullmatch(value) is None:
        raise BuildRejected(stage)
    whole, _, fractional = value[:-1].partition(".")
    try:
        parsed = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise BuildRejected(stage) from error
    if fractional:
        parsed = parsed.replace(microsecond=int((fractional + "000000")[:6]))
    return parsed


def _validate_materials(
    value: Any,
    *,
    stage: str,
    expected_materials: Mapping[str, str] | None = None,
) -> None:
    if type(value) is not list or not value:
        raise BuildRejected(stage + "_materials")
    docker_material = False
    docker_material_uris: set[str] = set()
    observed: dict[str, dict[str, str]] = {}
    for material in value:
        if type(material) is not dict:
            raise BuildRejected(stage + "_materials")
        uri = material.get("uri")
        digests = material.get("digest")
        if type(uri) is not str:
            raise BuildRejected(stage + "_materials")
        try:
            uri_size = len(uri.encode("utf-8", errors="strict"))
            decoded_uri = urllib.parse.unquote(
                uri,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            raise BuildRejected(stage + "_materials") from None
        if not 1 <= uri_size <= 2048 or type(digests) is not dict or not digests:
            raise BuildRejected(stage + "_materials")
        for algorithm, digest in digests.items():
            if (
                type(algorithm) is not str
                or _DIGEST_ALGORITHM_RE.fullmatch(algorithm) is None
                or type(digest) is not str
                or _HEX_DIGEST_VALUE_RE.fullmatch(digest) is None
            ):
                raise BuildRejected(stage + "_materials")
        if decoded_uri.casefold().startswith("pkg:docker/"):
            docker_material = True
            docker_material_uris.add(uri)
        if uri in observed:
            raise BuildRejected(stage + "_materials")
        observed[uri] = digests
    if not docker_material:
        raise BuildRejected(stage + "_materials")
    if expected_materials is not None:
        # BuildKit records exactly these three image inputs: the runtime
        # Debian manifest, reachable Dockerfile frontend, and SBOM scanner.
        # RUN network downloads are not provenance materials; the Dockerfiles
        # constrain apt with signed immutable snapshots and a final dpkg lock,
        # while this validator requires a non-reproducible predicate below.
        # Reject every other Docker PURL, including percent-escaped spellings.
        expected = {uri: {"sha256": digest.removeprefix("sha256:")} for uri, digest in expected_materials.items()}
        if docker_material_uris != set(expected_materials) or observed != expected:
            raise BuildRejected(stage + "_materials")


def _validate_slsa_predicate(
    value: Any,
    *,
    stage: str,
    expected_builder_id: str | None = None,
    expected_platform: str | None = None,
    expected_dockerfile: str | None = None,
    expected_parameters: Mapping[str, str] | None = None,
    expected_materials: Mapping[str, str] | None = None,
) -> None:
    """Validate the BuildKit SLSA v0.2 predicate, including run bindings."""

    if type(value) is not dict or value.get("buildType") != _SLSA_BUILD_TYPE:
        raise BuildRejected(stage + "_shape")
    builder = value.get("builder")
    invocation = value.get("invocation")
    metadata = value.get("metadata")
    build_config = value.get("buildConfig")
    materials = value.get("materials")
    if (
        type(builder) is not dict
        or type(builder.get("id")) is not str
        or type(invocation) is not dict
        or type(metadata) is not dict
        or type(build_config) is not dict
        or not build_config
    ):
        raise BuildRejected(stage + "_shape")
    if expected_builder_id is not None and builder["id"] != expected_builder_id:
        raise BuildRejected(stage + "_builder")
    _validate_materials(
        materials,
        stage=stage,
        expected_materials=expected_materials,
    )

    config_source = invocation.get("configSource")
    parameters = invocation.get("parameters")
    environment = invocation.get("environment")
    if (
        type(config_source) is not dict
        or type(config_source.get("entryPoint")) is not str
        or type(parameters) is not dict
        or parameters.get("frontend") not in {"dockerfile.v0", "gateway.v0"}
        or type(parameters.get("locals")) is not list
        or type(environment) is not dict
        or type(environment.get("platform")) is not str
    ):
        raise BuildRejected(stage + "_invocation")
    if set(parameters) != {"frontend", "args", "locals"}:
        raise BuildRejected(stage + "_invocation")
    local_rows = parameters["locals"]
    if len(local_rows) != 2 or any(
        type(row) is not dict or set(row) != {"name"} or type(row.get("name")) is not str for row in local_rows
    ):
        raise BuildRejected(stage + "_invocation")
    local_names = [row["name"] for row in local_rows]
    if sorted(local_names) != ["context", "dockerfile"]:
        raise BuildRejected(stage + "_invocation")
    if expected_platform is not None and environment["platform"] != expected_platform:
        raise BuildRejected(stage + "_platform")
    if expected_dockerfile is not None and config_source["entryPoint"] != expected_dockerfile:
        raise BuildRejected(stage + "_dockerfile")
    if expected_parameters is not None:
        arguments = parameters.get("args")
        if type(arguments) is not dict or arguments != dict(expected_parameters):
            raise BuildRejected(stage + "_parameters")

    completeness = metadata.get("completeness")
    started_at = _parse_rfc3339(
        metadata.get("buildStartedOn"),
        stage=stage + "_metadata",
    )
    finished_at = _parse_rfc3339(
        metadata.get("buildFinishedOn"),
        stage=stage + "_metadata",
    )
    if (
        type(metadata.get("buildInvocationID")) is not str
        or not metadata["buildInvocationID"]
        or finished_at < started_at
        # BuildKit does not attest bytes fetched by RUN. A true reproducible
        # value would overstate the signed-snapshot and package-lock boundary.
        or metadata.get("reproducible") is not False
        or type(completeness) is not dict
        or completeness.get("parameters") is not True
        or completeness.get("environment") is not True
        # BuildKit v0.32.2 sets this to false whenever Sources.Local is
        # nonempty. Our hosted stdin archive is deliberately a local source;
        # the hosted boundary proves it with the canonical archive
        # qualification digest, and the raw registry pass binds the exact
        # invocation parameters.
        or completeness.get("materials") is not False
    ):
        raise BuildRejected(stage + "_metadata")


def _validate_spdx_document(
    value: Any,
    *,
    stage: str,
    expected_components: tuple[tuple[str, str, str], ...],
    forbidden_component_names: frozenset[str] = frozenset(),
) -> None:
    """Validate Syft's registry SPDX document and its image-role components."""

    if (
        type(value) is not dict
        or value.get("SPDXID") != "SPDXRef-DOCUMENT"
        or value.get("name") != "sbom"
        or value.get("dataLicense") != "CC0-1.0"
        or value.get("spdxVersion") not in _SPDX_VERSIONS
        or type(value.get("documentNamespace")) is not str
        or not value["documentNamespace"].startswith("https://")
        or not expected_components
    ):
        raise BuildRejected(stage + "_shape")
    creation = value.get("creationInfo")
    packages = value.get("packages")
    files = value.get("files")
    relationships = value.get("relationships")
    if (
        type(creation) is not dict
        or type(creation.get("creators")) is not list
        or not creation["creators"]
        or any(type(item) is not str or not item for item in creation["creators"])
        or "Tool: syft-v1.42.3" not in creation["creators"]
        or type(packages) is not list
        or type(files) is not list
        or type(relationships) is not list
        or not packages
        or not relationships
    ):
        raise BuildRejected(stage + "_content")
    _parse_rfc3339(creation.get("created"), stage=stage + "_content")
    elements = {"SPDXRef-DOCUMENT"}
    packages_by_name: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        if (
            type(package) is not dict
            or type(package.get("SPDXID")) is not str
            or not package["SPDXID"].startswith("SPDXRef-")
            or type(package.get("name")) is not str
            or not package["name"]
            or package["SPDXID"] in elements
            or (package.get("externalRefs") is not None and type(package.get("externalRefs")) is not list)
        ):
            raise BuildRejected(stage + "_content")
        elements.add(package["SPDXID"])
        packages_by_name.setdefault(package["name"], []).append(package)
        for external_ref in package.get("externalRefs") or []:
            if (
                type(external_ref) is not dict
                or not {"referenceCategory", "referenceType", "referenceLocator"} <= set(external_ref)
                or type(external_ref.get("referenceCategory")) is not str
                or type(external_ref.get("referenceType")) is not str
                or type(external_ref.get("referenceLocator")) is not str
                or not external_ref["referenceLocator"]
            ):
                raise BuildRejected(stage + "_content")
    for file_row in files:
        if (
            type(file_row) is not dict
            or type(file_row.get("SPDXID")) is not str
            or not file_row["SPDXID"].startswith("SPDXRef-")
            or type(file_row.get("fileName")) is not str
            or not file_row["fileName"]
            or file_row["SPDXID"] in elements
        ):
            raise BuildRejected(stage + "_content")
        elements.add(file_row["SPDXID"])

    root_id = "SPDXRef-DocumentRoot-Directory-sbom"
    root_rows = [package for package in packages if package["SPDXID"] == root_id]
    if (
        len(root_rows) != 1
        or root_rows[0].get("name") != "sbom"
        or root_rows[0].get("primaryPackagePurpose") != "FILE"
        or len(packages_by_name.get("sbom", [])) != 1
    ):
        raise BuildRejected(stage + "_relationships")

    relationship_edges: set[tuple[str, str, str]] = set()
    describes_edges: list[tuple[str, str, str]] = []
    for relationship in relationships:
        if (
            type(relationship) is not dict
            or not {
                "spdxElementId",
                "relatedSpdxElement",
                "relationshipType",
            }
            <= set(relationship)
            or not set(relationship)
            <= {
                "spdxElementId",
                "relatedSpdxElement",
                "relationshipType",
                "comment",
            }
            or type(relationship.get("spdxElementId")) is not str
            or type(relationship.get("relatedSpdxElement")) is not str
            or type(relationship.get("relationshipType")) is not str
            or relationship["spdxElementId"] not in elements
            or relationship["relatedSpdxElement"] not in elements
        ):
            raise BuildRejected(stage + "_relationships")
        edge = (
            relationship["spdxElementId"],
            relationship["relatedSpdxElement"],
            relationship["relationshipType"],
        )
        if edge in relationship_edges:
            raise BuildRejected(stage + "_relationships")
        relationship_edges.add(edge)
        if relationship["relationshipType"] == "DESCRIBES":
            describes_edges.append(edge)
    if describes_edges != [("SPDXRef-DOCUMENT", root_id, "DESCRIBES")]:
        raise BuildRejected(stage + "_relationships")

    # Syft v1.42.3 creates one root CONTAINS edge for every discovered
    # package. Requiring those edges prevents a matching-but-unrelated package
    # list from being spliced into an otherwise valid document envelope.
    for package in packages:
        package_id = package["SPDXID"]
        if package_id != root_id and (root_id, package_id, "CONTAINS") not in relationship_edges:
            raise BuildRejected(stage + "_relationships")

    if forbidden_component_names & packages_by_name.keys():
        raise BuildRejected(stage + "_components")
    for name, version, purl in expected_components:
        matches = packages_by_name.get(name, [])
        if len(matches) != 1 or matches[0].get("versionInfo") != version:
            raise BuildRejected(stage + "_components")
        purl_refs = [
            external_ref
            for external_ref in matches[0].get("externalRefs") or []
            if external_ref.get("referenceType") == "purl"
        ]
        if purl_refs != [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl,
            }
        ]:
            raise BuildRejected(stage + "_components")


def _validated_build_metadata(path: Path) -> tuple[dict[str, Any], str, str, str]:
    """Validate Buildx's OCI-index result and return index/config identities."""

    metadata, metadata_digest = _strict_build_metadata(path)
    manifest_digest = metadata.get("containerimage.digest")
    config_digest = metadata.get("containerimage.config.digest")
    descriptor = metadata.get("containerimage.descriptor")
    provenance = metadata.get("buildx.build.provenance")
    if (
        type(manifest_digest) is not str
        or _DIGEST_RE.fullmatch(manifest_digest) is None
        or type(config_digest) is not str
        or _DIGEST_RE.fullmatch(config_digest) is None
        or type(descriptor) is not dict
        or descriptor.get("digest") != manifest_digest
        or descriptor.get("mediaType") != "application/vnd.oci.image.index.v1+json"
        or type(descriptor.get("size")) is not int
        or not 1 <= descriptor["size"] <= (1 << 53) - 1
        or type(provenance) is not dict
    ):
        raise BuildRejected("build_metadata_identity")
    _validate_slsa_predicate(provenance, stage="build_metadata_provenance")
    annotations = descriptor.get("annotations")
    if annotations is not None:
        if type(annotations) is not dict:
            raise BuildRejected("build_metadata_identity")
        annotated_config = annotations.get("config.digest")
        if annotated_config is not None and annotated_config != config_digest:
            raise BuildRejected("build_metadata_identity")
    return metadata, metadata_digest, manifest_digest, config_digest


def _strict_registry_json(payload: bytes, *, maximum: int, stage: str) -> Any:
    if not 1 <= len(payload) <= maximum:
        raise BuildRejected(stage + "_size")

    def reject_number(_value: str) -> None:
        raise BuildRejected(stage + "_number")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            if key in row:
                raise BuildRejected(stage + "_duplicate_key")
            row[key] = value
        return row

    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildRejected(stage + "_json") from error


def _read_docker_authorization(*, stage: str) -> str:
    """Read Docker's isolated GHCR login without logging or exporting it."""

    config_root = os.environ.get("DOCKER_CONFIG", "")
    if not config_root or not Path(config_root).is_absolute():
        raise BuildRejected(stage + "_credentials")
    path = Path(config_root) / "config.json"
    try:
        info = path.lstat()
    except OSError as error:
        raise BuildRejected(stage + "_credentials") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or not 1 <= info.st_size <= MAX_DOCKER_CONFIG_BYTES
    ):
        raise BuildRejected(stage + "_credentials")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_mode & 0o077
            or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size)
        ):
            raise BuildRejected(stage + "_credentials")
        payload = os.read(descriptor, opened.st_size + 1)
        after = os.fstat(descriptor)
        if len(payload) != opened.st_size or (
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
            raise BuildRejected(stage + "_credentials")
    except OSError as error:
        raise BuildRejected(stage + "_credentials") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    document = _strict_registry_json(
        payload,
        maximum=MAX_DOCKER_CONFIG_BYTES,
        stage=stage + "_credentials",
    )
    if type(document) is not dict or type(document.get("auths")) is not dict:
        raise BuildRejected(stage + "_credentials")
    auth = document["auths"].get("ghcr.io")
    if type(auth) is not dict or type(auth.get("auth")) is not str:
        raise BuildRejected(stage + "_credentials")
    encoded = auth["auth"]
    if not 1 <= len(encoded) <= 24 * 1024:
        raise BuildRejected(stage + "_credentials")
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise BuildRejected(stage + "_credentials") from error
    username, separator, secret = decoded.partition(b":")
    if not separator or not username or not secret or b"\x00" in decoded:
        raise BuildRejected(stage + "_credentials")
    return "Basic " + encoded


def _parse_bearer_challenge(value: str, *, repository: str, stage: str) -> str:
    if type(value) is not str or not value.startswith("Bearer "):
        raise BuildRejected(stage + "_authentication")
    fields: dict[str, str] = {}
    remainder = value[7:]
    for part in remainder.split(","):
        key, separator, quoted = part.strip().partition("=")
        if (
            not separator
            or key in fields
            or len(quoted) < 2
            or quoted[0] != '"'
            or quoted[-1] != '"'
            or '"' in quoted[1:-1]
        ):
            raise BuildRejected(stage + "_authentication")
        fields[key] = quoted[1:-1]
    if fields != {
        "realm": "https://ghcr.io/token",
        "service": "ghcr.io",
        "scope": "repository:" + repository + ":pull",
    }:
        raise BuildRejected(stage + "_authentication")
    return fields["realm"] + "?" + urllib.parse.urlencode({"service": fields["service"], "scope": fields["scope"]})


def _open_registry_request(
    request: urllib.request.Request,
    *,
    maximum: int,
    stage: str,
    allow_auth_challenge: bool = False,
) -> tuple[bytes | None, str | None]:
    """Read one bounded HTTPS response; redirects never inherit credentials."""

    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    current = request
    for _ in range(4):
        try:
            response = opener.open(current, timeout=RELEASE_FETCH_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            code = error.code
            challenge = error.headers.get("WWW-Authenticate")
            location = error.headers.get("Location", "")
            error.close()
            if code == 401 and allow_auth_challenge:
                return None, challenge
            if code not in {301, 302, 303, 307, 308}:
                raise BuildRejected(stage + "_response") from error
            parsed = urllib.parse.urlsplit(location)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise BuildRejected(stage + "_redirect") from error
            current = urllib.request.Request(
                location,
                headers={
                    "Accept": request.headers.get("Accept", "application/octet-stream"),
                    "Accept-Encoding": "identity",
                    "User-Agent": "algo-cli-boron-hardening/1",
                },
                method="GET",
            )
            continue
        with response:
            if response.status != 200:
                raise BuildRejected(stage + "_response")
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.casefold() != "identity":
                raise BuildRejected(stage + "_encoding")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise BuildRejected(stage + "_size") from error
                if not 1 <= declared_size <= maximum:
                    raise BuildRejected(stage + "_size")
            payload = response.read(maximum + 1)
            if not 1 <= len(payload) <= maximum:
                raise BuildRejected(stage + "_size")
            return payload, None
    raise BuildRejected(stage + "_redirect")


def _registry_blob_bytes(
    repository: str,
    *,
    digest: str,
    expected_size: int,
    stage: str,
) -> bytes:
    """Fetch one GHCR blob by immutable digest using the isolated Docker login."""

    if (
        _REGISTRY_RE.fullmatch("ghcr.io/" + repository) is None
        or _DIGEST_RE.fullmatch(digest) is None
        or type(expected_size) is not int
        or not 1 <= expected_size <= MAX_REGISTRY_ATTESTATION_BYTES
    ):
        raise BuildRejected(stage + "_identity")
    basic = _read_docker_authorization(stage=stage)
    blob_url = "https://ghcr.io/v2/" + repository + "/blobs/" + digest
    request = urllib.request.Request(
        blob_url,
        headers={
            "Accept": _INTOTO_MEDIA_TYPE,
            "Accept-Encoding": "identity",
            "Authorization": basic,
            "User-Agent": "algo-cli-boron-hardening/1",
        },
        method="GET",
    )
    payload, challenge = _open_registry_request(
        request,
        maximum=expected_size,
        stage=stage,
        allow_auth_challenge=True,
    )
    if payload is None:
        token_url = _parse_bearer_challenge(
            challenge or "",
            repository=repository,
            stage=stage,
        )
        token_request = urllib.request.Request(
            token_url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": basic,
                "User-Agent": "algo-cli-boron-hardening/1",
            },
            method="GET",
        )
        token_payload, token_challenge = _open_registry_request(
            token_request,
            maximum=64 * 1024,
            stage=stage + "_token",
        )
        if token_payload is None or token_challenge is not None:
            raise BuildRejected(stage + "_authentication")
        token_document = _strict_registry_json(
            token_payload,
            maximum=64 * 1024,
            stage=stage + "_token",
        )
        if type(token_document) is not dict:
            raise BuildRejected(stage + "_authentication")
        token = token_document.get("token", token_document.get("access_token"))
        if type(token) is not str or not 1 <= len(token) <= 32 * 1024:
            raise BuildRejected(stage + "_authentication")
        payload, challenge = _open_registry_request(
            urllib.request.Request(
                blob_url,
                headers={
                    "Accept": _INTOTO_MEDIA_TYPE,
                    "Accept-Encoding": "identity",
                    "Authorization": "Bearer " + token,
                    "User-Agent": "algo-cli-boron-hardening/1",
                },
                method="GET",
            ),
            maximum=expected_size,
            stage=stage,
        )
    if payload is None or challenge is not None or len(payload) != expected_size:
        raise BuildRejected(stage + "_size")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise BuildRejected(stage + "_digest")
    return payload


def _registry_index_descriptors(
    reference: str,
    *,
    platform: str,
    expected_size: int,
    stage: str,
) -> tuple[str, int, str, int]:
    """Resolve the platform and its sole attestation manifest from raw OCI bytes."""

    if (
        "@" not in reference
        or _REGISTRY_RE.fullmatch(reference.rsplit("@", 1)[0]) is None
        or _DIGEST_RE.fullmatch(reference.rsplit("@", 1)[1]) is None
        or platform != PLATFORM
        or type(expected_size) is not int
        or not 1 <= expected_size <= MAX_BUILD_METADATA_BYTES
    ):
        raise BuildRejected(stage + "_identity")
    result = _run(
        ["docker", "buildx", "imagetools", "inspect", reference, "--raw"],
        stage=stage,
        timeout=120,
    )
    payload = result.stdout.encode("utf-8", errors="strict")
    index_digest = reference.rsplit("@", 1)[1]
    if len(payload) != expected_size or "sha256:" + hashlib.sha256(payload).hexdigest() != index_digest:
        raise BuildRejected(stage + "_digest")
    document = _strict_registry_json(
        payload,
        maximum=MAX_BUILD_METADATA_BYTES,
        stage=stage,
    )
    if (
        type(document) is not dict
        or document.get("schemaVersion") != 2
        or document.get("mediaType") != _OCI_INDEX_MEDIA_TYPE
        or type(document.get("manifests")) is not list
        or len(document["manifests"]) != 2
    ):
        raise BuildRejected(stage + "_shape")
    selected: list[tuple[str, int]] = []
    attestations: list[tuple[str, int, str]] = []
    for raw in document["manifests"]:
        if type(raw) is not dict:
            raise BuildRejected(stage + "_shape")
        digest = raw.get("digest")
        media_type = raw.get("mediaType")
        size = raw.get("size")
        target = raw.get("platform")
        annotations = raw.get("annotations", {})
        if (
            type(digest) is not str
            or _DIGEST_RE.fullmatch(digest) is None
            or media_type != _OCI_MANIFEST_MEDIA_TYPE
            or type(size) is not int
            or not 1 <= size <= (1 << 53) - 1
            or type(target) is not dict
            or type(annotations) is not dict
            or any(type(key) is not str or type(value) is not str for key, value in annotations.items())
        ):
            raise BuildRejected(stage + "_shape")
        if target == {"os": "linux", "architecture": "amd64"}:
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                raise BuildRejected(stage + "_shape")
            selected.append((digest, size))
        elif (
            target == {"os": "unknown", "architecture": "unknown"}
            and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
        ):
            attestations.append((digest, size, annotations.get("vnd.docker.reference.digest", "")))
        else:
            raise BuildRejected(stage + "_shape")
    if len(selected) != 1 or len(attestations) != 1 or attestations[0][2] != selected[0][0]:
        raise BuildRejected(stage + "_attestation_binding")
    return selected[0][0], selected[0][1], attestations[0][0], attestations[0][1]


def _registry_subject_name(tag: str, *, platform: str) -> str:
    if _REGISTRY_TAG_RE.fullmatch(tag) is None or platform != PLATFORM:
        raise BuildRejected("registry_subject_identity")
    name, version = tag.rsplit(":", 1)
    return "pkg:docker/" + name + "@" + version + "?platform=linux%2Famd64"


def _registry_attestation_digests(
    *,
    tag: str,
    platform_manifest_digest: str,
    platform_manifest_size: int,
    attestation_manifest_digest: str,
    attestation_manifest_size: int,
    stage: str,
    expected_builder_id: str,
    expected_platform: str,
    expected_dockerfile: str,
    expected_parameters: Mapping[str, str],
) -> tuple[str, str]:
    """Validate raw in-toto layers and return their immutable descriptor digests."""

    repository = tag.rsplit(":", 1)[0]
    expected_components = _SBOM_COMPONENTS_BY_DOCKERFILE.get(expected_dockerfile)
    forbidden_components = _SBOM_FORBIDDEN_COMPONENTS_BY_DOCKERFILE.get(expected_dockerfile)
    expected_repository = _REGISTRY_REPOSITORY_BY_DOCKERFILE.get(expected_dockerfile)
    if (
        _REGISTRY_TAG_RE.fullmatch(tag) is None
        or expected_components is None
        or forbidden_components is None
        or repository != expected_repository
        or _DIGEST_RE.fullmatch(platform_manifest_digest) is None
        or type(platform_manifest_size) is not int
        or not 1 <= platform_manifest_size <= (1 << 53) - 1
        or _DIGEST_RE.fullmatch(attestation_manifest_digest) is None
        or type(attestation_manifest_size) is not int
        or not 1 <= attestation_manifest_size <= MAX_BUILD_METADATA_BYTES
    ):
        raise BuildRejected(stage + "_identity")
    attestation_reference = repository + "@" + attestation_manifest_digest
    result = _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            attestation_reference,
            "--raw",
        ],
        stage=stage + "_manifest",
        timeout=120,
    )
    manifest_payload = result.stdout.encode("utf-8", errors="strict")
    if (
        len(manifest_payload) != attestation_manifest_size
        or "sha256:" + hashlib.sha256(manifest_payload).hexdigest() != attestation_manifest_digest
    ):
        raise BuildRejected(stage + "_manifest_digest")
    manifest = _strict_registry_json(
        manifest_payload,
        maximum=MAX_BUILD_METADATA_BYTES,
        stage=stage + "_manifest",
    )
    expected_subject: dict[str, Any] = {
        "mediaType": _OCI_MANIFEST_MEDIA_TYPE,
        "digest": platform_manifest_digest,
        "size": platform_manifest_size,
    }
    observed_subject = manifest.get("subject") if type(manifest) is dict else None
    # BuildKit v0.32.2's OCI-artifact writer binds digest/mediaType/size and
    # omits platform; Docker's documented registry form may also preserve the
    # selected descriptor's platform. Accept only those two authoritative
    # shapes, and require exact linux/amd64 whenever the field is present.
    if type(observed_subject) is dict and "platform" in observed_subject:
        expected_subject["platform"] = {
            "architecture": "amd64",
            "os": "linux",
        }
    expected_config = {
        "mediaType": _OCI_EMPTY_MEDIA_TYPE,
        "digest": _EMPTY_JSON_DIGEST,
        "size": 2,
        "data": "e30=",
    }
    if (
        type(manifest) is not dict
        or set(manifest) != {"schemaVersion", "mediaType", "artifactType", "config", "layers", "subject"}
        or manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != _OCI_MANIFEST_MEDIA_TYPE
        or manifest.get("artifactType") != _ATTESTATION_ARTIFACT_TYPE
        or manifest.get("config") != expected_config
        or observed_subject != expected_subject
        or type(manifest.get("layers")) is not list
        or len(manifest["layers"]) != 2
    ):
        raise BuildRejected(stage + "_manifest_shape")
    expected_name = _registry_subject_name(tag, platform=expected_platform)
    digests: dict[str, str] = {}
    for layer in manifest["layers"]:
        if type(layer) is not dict:
            raise BuildRejected(stage + "_manifest_shape")
        digest = layer.get("digest")
        size = layer.get("size")
        annotations = layer.get("annotations")
        if (
            set(layer) != {"mediaType", "digest", "size", "annotations"}
            or layer.get("mediaType") != _INTOTO_MEDIA_TYPE
            or type(digest) is not str
            or _DIGEST_RE.fullmatch(digest) is None
            or type(size) is not int
            or not 1 <= size <= MAX_REGISTRY_ATTESTATION_BYTES
            or type(annotations) is not dict
            or set(annotations) != {"in-toto.io/predicate-type"}
        ):
            raise BuildRejected(stage + "_manifest_shape")
        predicate_type = annotations["in-toto.io/predicate-type"]
        if predicate_type not in {_SLSA_PREDICATE_TYPE, _SPDX_PREDICATE_TYPE}:
            raise BuildRejected(stage + "_predicate_type")
        if predicate_type in digests:
            raise BuildRejected(stage + "_attestation_count")
        statement_payload = _registry_blob_bytes(
            repository.removeprefix("ghcr.io/"),
            digest=digest,
            expected_size=size,
            stage=(stage + "_provenance" if predicate_type == _SLSA_PREDICATE_TYPE else stage + "_sbom"),
        )
        if len(statement_payload) != size or "sha256:" + hashlib.sha256(statement_payload).hexdigest() != digest:
            raise BuildRejected(stage + "_statement_digest")
        statement = _strict_registry_json(
            statement_payload,
            maximum=MAX_REGISTRY_ATTESTATION_BYTES,
            stage=(stage + "_provenance" if predicate_type == _SLSA_PREDICATE_TYPE else stage + "_sbom"),
        )
        if (
            type(statement) is not dict
            or set(statement) != {"_type", "subject", "predicateType", "predicate"}
            or statement.get("_type") != _INTOTO_STATEMENT_TYPE
            or statement.get("predicateType") != predicate_type
            or statement.get("subject")
            != [
                {
                    "name": expected_name,
                    "digest": {"sha256": platform_manifest_digest.removeprefix("sha256:")},
                }
            ]
            or type(statement.get("predicate")) is not dict
        ):
            raise BuildRejected(stage + "_statement_binding")
        if predicate_type == _SLSA_PREDICATE_TYPE:
            _validate_slsa_predicate(
                statement["predicate"],
                stage=stage + "_provenance",
                expected_builder_id=expected_builder_id,
                expected_platform=expected_platform,
                expected_dockerfile=expected_dockerfile,
                expected_parameters=expected_parameters,
                expected_materials=_PINNED_PROVENANCE_MATERIALS,
            )
        else:
            _validate_spdx_document(
                statement["predicate"],
                stage=stage + "_sbom",
                expected_components=expected_components,
                forbidden_component_names=forbidden_components,
            )
        digests[predicate_type] = digest
    if set(digests) != {_SLSA_PREDICATE_TYPE, _SPDX_PREDICATE_TYPE}:
        raise BuildRejected(stage + "_attestation_count")
    return digests[_SLSA_PREDICATE_TYPE], digests[_SPDX_PREDICATE_TYPE]


def _published_build(
    *,
    context_archive: bytes,
    qualification_source_digest: str,
    dockerfile: str,
    tag: str,
    build_arg: str,
    stage: str,
    platform: str,
    labels: tuple[str, ...],
    builder_id: str,
) -> tuple[str, str, str, str, str, str]:
    """Push one provenance-bearing image and pull only its returned digest."""

    _validated_build_context_archive(
        context_archive,
        qualification_source_digest=qualification_source_digest,
    )
    if (
        _REGISTRY_TAG_RE.fullmatch(tag) is None
        or platform != PLATFORM
        or dockerfile
        not in {
            "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
            "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile",
        }
        or not labels
        or any(type(label) is not str or "=" not in label for label in labels)
        or type(builder_id) is not str
        or not builder_id.startswith("https://github.com/Seabass-up/Algo-cli/actions/runs/")
    ):
        raise BuildRejected("registry_tag")
    label_rows = [label.split("=", 1) for label in labels]
    label_map = dict(label_rows)
    if len(label_map) != len(label_rows):
        raise BuildRejected("registry_source_label")
    required_labels = {
        "org.opencontainers.image.source",
        "org.opencontainers.image.revision",
        "com.algo-cli.github.repository-id",
        "com.algo-cli.github.run-id",
        "com.algo-cli.github.run-attempt",
        "com.algo-cli.qualification.source.sha256",
    }
    if set(label_map) != required_labels:
        raise BuildRejected("registry_source_label")
    label_revision = label_map["org.opencontainers.image.revision"]
    label_run_id = label_map["com.algo-cli.github.run-id"]
    label_attempt = label_map["com.algo-cli.github.run-attempt"]
    if (
        label_map["org.opencontainers.image.source"] != "https://github.com/" + HOSTED_REPOSITORY
        or _REVISION_RE.fullmatch(label_revision) is None
        or label_map["com.algo-cli.github.repository-id"] != HOSTED_REPOSITORY_ID
        or _INTEGER_RE.fullmatch(label_run_id) is None
        or _INTEGER_RE.fullmatch(label_attempt) is None
        or _DIGEST_RE.fullmatch(label_map["com.algo-cli.qualification.source.sha256"]) is None
        or not tag.endswith(f":run-{label_run_id}-{label_attempt}-{label_revision}")
    ):
        raise BuildRejected("registry_source_label")
    expected_builder_id = (
        "https://github.com/" + HOSTED_REPOSITORY + "/actions/runs/" + label_run_id + "/attempts/" + label_attempt
    )
    if builder_id != expected_builder_id:
        raise BuildRejected("registry_builder_identity")
    try:
        build_arg_name, build_arg_value = build_arg.split("=", 1)
    except ValueError as error:
        raise BuildRejected("registry_build_arg") from error
    if (
        build_arg_name not in {"BORON_CODE_DIGEST", "XENON_CODE_DIGEST"}
        or _DIGEST_RE.fullmatch(build_arg_value) is None
    ):
        raise BuildRejected("registry_build_arg")
    expected_parameters = {
        **{"label:" + key: value for key, value in label_map.items()},
        "build-arg:" + build_arg_name: build_arg_value,
    }
    descriptor, metadata_name = tempfile.mkstemp(
        prefix="henry-boron-build-",
        suffix=".json",
    )
    os.close(descriptor)
    metadata_path = Path(metadata_name)
    try:
        command = [
            "docker",
            "buildx",
            "build",
            "--platform",
            platform,
            "--pull",
            "--output=type=registry,oci-mediatypes=true,oci-artifact=true",
            "--provenance=mode=max,version=v0.2,builder-id=" + builder_id,
            "--sbom=generator=" + SBOM_GENERATOR_REFERENCE,
            "--metadata-file",
            str(metadata_path),
            "--file",
            dockerfile,
            "--build-arg",
            build_arg,
            "--tag",
            tag,
        ]
        for label in labels:
            command.extend(("--label", label))
        # Pinned Buildx v0.36.1 loadInputs treats archive stdin plus this
        # relative --file as one uploaded context; it does not mount the
        # checkout as either context or Dockerfile input.
        command.append("-")
        _run(
            command,
            stage=stage,
            environment={
                "BUILDX_GIT_INFO": "true",
                "BUILDX_GIT_LABELS": "full",
                "BUILDX_METADATA_PROVENANCE": "max",
            },
            input_bytes=context_archive,
        )
        (
            metadata,
            metadata_digest,
            manifest_digest,
            metadata_config_digest,
        ) = _validated_build_metadata(metadata_path)
    finally:
        primary_error = sys.exc_info()[1]
        try:
            metadata_path.unlink()
        except OSError as error:
            reason = "build_metadata_cleanup" if primary_error is None else "build_failed_and_metadata_cleanup"
            raise BuildRejected(reason) from (primary_error or error)
    repository = tag.rsplit(":", 1)[0]
    reference = repository + "@" + manifest_digest
    descriptor = metadata["containerimage.descriptor"]
    (
        platform_manifest_digest,
        platform_manifest_size,
        attestation_manifest_digest,
        attestation_manifest_size,
    ) = _registry_index_descriptors(
        reference,
        platform=platform,
        expected_size=descriptor["size"],
        stage=stage + "_index",
    )
    _run(
        ["docker", "pull", "--platform", platform, reference],
        stage=stage + "_pull",
    )
    inspected = _inspect(reference)
    config_digest = inspected.get("Id")
    repo_digests = inspected.get("RepoDigests")
    if (
        type(config_digest) is not str
        or _DIGEST_RE.fullmatch(config_digest) is None
        or config_digest != metadata_config_digest
        or type(repo_digests) is not list
        or reference not in repo_digests
    ):
        raise BuildRejected("registry_pull_identity")
    provenance_digest, sbom_digest = _registry_attestation_digests(
        tag=tag,
        platform_manifest_digest=platform_manifest_digest,
        platform_manifest_size=platform_manifest_size,
        attestation_manifest_digest=attestation_manifest_digest,
        attestation_manifest_size=attestation_manifest_size,
        stage=stage + "_attestations",
        expected_builder_id=builder_id,
        expected_platform=platform,
        expected_dockerfile=dockerfile,
        expected_parameters=expected_parameters,
    )
    return (
        reference,
        platform_manifest_digest,
        config_digest,
        metadata_digest,
        provenance_digest,
        sbom_digest,
    )


def build_images(
    *,
    now_ms: int | None = None,
    release_evidence: BoronBrowserReleaseEvidence | None = None,
    include_unverified_native_browser: bool = False,
    hosted_environment: Mapping[str, str] | None = None,
    qualification_source_digest: str | None = None,
    context_root: Path | None = None,
    context_archive: bytes | None = None,
) -> dict[str, Any]:
    if now_ms is not None and (type(now_ms) is not int or now_ms < 1):
        raise BuildRejected("now_ms")
    if type(include_unverified_native_browser) is not bool:
        raise BuildRejected("native_browser_option")
    if hosted_environment is not None and include_unverified_native_browser:
        raise BuildRejected("native_browser_hosted_forbidden")
    if (hosted_environment is None and qualification_source_digest is not None) or (
        hosted_environment is not None
        and (type(qualification_source_digest) is not str or _DIGEST_RE.fullmatch(qualification_source_digest) is None)
    ):
        raise BuildRejected("qualification_source_digest")
    registry_mode = hosted_environment is not None
    if registry_mode:
        if context_root is not None or context_archive is None or qualification_source_digest is None:
            raise BuildRejected("registry_build_context")
        context_payloads = _validated_build_context_archive(
            context_archive,
            qualification_source_digest=qualification_source_digest,
        )
        build_root = ROOT
    else:
        if context_archive is not None:
            raise BuildRejected("build_context")
        context_payloads = None
        if context_root is not None:
            try:
                build_root = context_root.resolve(strict=True)
            except OSError as error:
                raise BuildRejected("build_context") from error
            if not build_root.is_dir():
                raise BuildRejected("build_context")
        else:
            build_root = ROOT
    browser_code_paths = tuple(build_root / path.relative_to(ROOT) for path in BROWSER_CODE)
    broker_code_paths = tuple(build_root / path.relative_to(ROOT) for path in BROKER_CODE)
    authoritative_release = fetch_browser_release_evidence() if release_evidence is None else release_evidence
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

    if context_payloads is None:
        browser_code_digest = _tree_digest(browser_code_paths, root=build_root)
        broker_code_digest = _tree_digest(broker_code_paths, root=build_root)
    else:
        browser_code_digest = _payload_tree_digest(
            BROWSER_CODE,
            payloads=context_payloads,
        )
        broker_code_digest = _payload_tree_digest(
            BROKER_CODE,
            payloads=context_payloads,
        )
    if hosted_environment is not None:
        browser_tag, broker_tag = hosted_registry_tags(hosted_environment)
    else:
        browser_tag, broker_tag = BROWSER_TAG, BROKER_TAG
    builds = [
        (
            "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
            browser_tag,
            "BORON_CODE_DIGEST=" + browser_code_digest,
            "browser_build",
            PLATFORM,
        ),
        (
            "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile",
            broker_tag,
            "XENON_CODE_DIGEST=" + broker_code_digest,
            "broker_build",
            PLATFORM,
        ),
    ]
    if include_unverified_native_browser:
        builds.insert(
            1,
            (
                "algo_cli/resources/boron_browser/carbon_native_browser.Dockerfile",
                NATIVE_BROWSER_TAG,
                "BORON_CODE_DIGEST=" + browser_code_digest,
                "native_browser_build",
                "linux/arm64",
            ),
        )
    published: dict[str, tuple[str, str, str, str, str, str]] = {}
    hosted_labels: tuple[str, ...] = ()
    builder_id = ""
    if hosted_environment is not None:
        if qualification_source_digest is None:
            raise BuildRejected("qualification_source_digest")
        hosted_labels = (
            "org.opencontainers.image.source=https://github.com/" + HOSTED_REPOSITORY,
            "org.opencontainers.image.revision=" + hosted_environment["GITHUB_SHA"],
            "com.algo-cli.github.repository-id=" + HOSTED_REPOSITORY_ID,
            "com.algo-cli.github.run-id=" + hosted_environment["GITHUB_RUN_ID"],
            "com.algo-cli.github.run-attempt=" + hosted_environment["GITHUB_RUN_ATTEMPT"],
            "com.algo-cli.qualification.source.sha256=" + qualification_source_digest,
        )
        builder_id = (
            "https://github.com/"
            + HOSTED_REPOSITORY
            + "/actions/runs/"
            + hosted_environment["GITHUB_RUN_ID"]
            + "/attempts/"
            + hosted_environment["GITHUB_RUN_ATTEMPT"]
        )
    for dockerfile, tag, build_arg, stage, build_platform in builds:
        if registry_mode:
            if context_archive is None or qualification_source_digest is None:
                raise BuildRejected("registry_build_context")
            published[tag] = _published_build(
                context_archive=context_archive,
                qualification_source_digest=qualification_source_digest,
                dockerfile=dockerfile,
                tag=tag,
                build_arg=build_arg,
                stage=stage,
                platform=build_platform,
                labels=hosted_labels,
                builder_id=builder_id,
            )
        else:
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
                    dockerfile,
                    "--build-arg",
                    build_arg,
                    "--tag",
                    tag,
                    ".",
                ],
                stage=stage,
                working_directory=build_root,
            )

    browser_reference = published.get(browser_tag, (browser_tag, "", "", "", "", ""))[0]
    broker_reference = published.get(broker_tag, (broker_tag, "", "", "", "", ""))[0]
    browser = _inspect(browser_reference)
    broker = _inspect(broker_reference)
    browser_labels = _labels(browser)
    broker_labels = _labels(broker)
    shared_supply_chain_labels = {
        "com.algo-cli.debian.snapshot": DEBIAN_SNAPSHOT,
        "com.algo-cli.debian.security-snapshot": DEBIAN_SECURITY_SNAPSHOT,
        "com.algo-cli.build.hermetic": "false",
        "com.algo-cli.build.reproducible": "false",
    }
    browser_supply_chain_labels = {
        **shared_supply_chain_labels,
        "com.algo-cli.dpkg.lock.sha256": BROWSER_DPKG_LOCK_DIGEST,
        "com.algo-cli.dpkg.lock.entries": BROWSER_DPKG_LOCK_ENTRIES,
    }
    broker_supply_chain_labels = {
        **shared_supply_chain_labels,
        "com.algo-cli.dpkg.lock.sha256": BROKER_DPKG_LOCK_DIGEST,
        "com.algo-cli.dpkg.lock.entries": BROKER_DPKG_LOCK_ENTRIES,
    }
    if (
        browser_labels.get("com.algo-cli.role") != "managed-browser"
        or browser_labels.get("com.algo-cli.code.sha256") != browser_code_digest
        or browser_labels.get("com.algo-cli.browser.version") != CHROME_VERSION
        or browser_labels.get("com.algo-cli.browser.release-at-ms") != str(CHROME_RELEASE_AT_MS)
        or broker_labels.get("com.algo-cli.role") != "egress-broker"
        or broker_labels.get("com.algo-cli.code.sha256") != broker_code_digest
        or any(browser_labels.get(key) != value for key, value in browser_supply_chain_labels.items())
        or any(broker_labels.get(key) != value for key, value in broker_supply_chain_labels.items())
        or browser.get("Architecture") != "amd64"
        or broker.get("Architecture") != "amd64"
        or browser.get("Config", {}).get("User") != "1000:1000"
        or broker.get("Config", {}).get("User") != "1001:1001"
    ):
        raise BuildRejected("image_identity_mismatch")
    if registry_mode:
        expected_labels = dict(label.split("=", 1) for label in hosted_labels)
        browser_repo_digests = browser.get("RepoDigests")
        broker_repo_digests = broker.get("RepoDigests")
        if any(
            browser_labels.get(key) != value or broker_labels.get(key) != value
            for key, value in expected_labels.items()
        ):
            raise BuildRejected("registry_source_label_mismatch")
        if (
            browser.get("Id") != published[browser_tag][2]
            or broker.get("Id") != published[broker_tag][2]
            or type(browser_repo_digests) is not list
            or published[browser_tag][0] not in browser_repo_digests
            or type(broker_repo_digests) is not list
            or published[broker_tag][0] not in broker_repo_digests
        ):
            raise BuildRejected("registry_reinspection_mismatch")

    native_browser: dict[str, Any] | None = None
    if include_unverified_native_browser:
        native_browser = _inspect(NATIVE_BROWSER_TAG)
        native_labels = _labels(native_browser)
        if (
            native_labels.get("com.algo-cli.role") != "managed-browser"
            or native_labels.get("com.algo-cli.code.sha256") != browser_code_digest
            or native_labels.get("com.algo-cli.browser.family") != "chromium_stable"
            or native_labels.get("com.algo-cli.browser.version") != NATIVE_CHROMIUM_VERSION
            or native_labels.get("com.algo-cli.browser.release-at-ms") != str(NATIVE_CHROMIUM_RELEASE_AT_MS)
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
            browser_reference,
            "--version",
        ],
        stage="browser_version_probe",
        timeout=60,
    ).stdout.strip()
    if chrome_version != "Google Chrome " + CHROME_VERSION:
        raise BuildRejected("browser_version_mismatch")
    module_probes = [
        (browser_reference, "algo_cli.boron_browser_entry"),
        (broker_reference, "algo_cli.xenon_browser_entry"),
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
                f"import cryptography; import {module}; assert cryptography.__version__ == '{CRYPTOGRAPHY_VERSION}'",
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
        "schema_version": 2,
        "platform": PLATFORM,
        "qualification_source_digest": qualification_source_digest,
        "browser_tag": browser_tag,
        "browser_repository": browser_tag.rsplit(":", 1)[0],
        "browser_index_digest": (published[browser_tag][0].rsplit("@", 1)[1] if registry_mode else None),
        "browser_platform_manifest_digest": (published[browser_tag][1] if registry_mode else None),
        "browser_config_digest": browser.get("Id"),
        "browser_build_metadata_digest": (published[browser_tag][3] if registry_mode else None),
        "browser_provenance_digest": (published[browser_tag][4] if registry_mode else None),
        "browser_sbom_digest": published[browser_tag][5] if registry_mode else None,
        "browser_code_digest": browser_code_digest,
        "browser_version": CHROME_VERSION,
        "browser_security_update_lag_ms": update_lag_ms,
        "browser_security_max_update_lag_ms": BORON_MAX_SECURITY_LAG_MS,
        "browser_security_latest_version": authoritative_release.browser_version,
        "browser_security_latest_release_at_ms": (authoritative_release.security_release_at_ms),
        "browser_security_evidence_observed_at_ms": (authoritative_release.observed_at_ms),
        "browser_security_source": authoritative_release.source.value,
        "browser_security_source_digest": authoritative_release.source_digest,
        "native_browser_built": include_unverified_native_browser,
        "native_browser_fresh": False,
        "native_browser_freshness_reason": "upstream_patch_equivalence_unverified",
        "broker_tag": broker_tag,
        "broker_repository": broker_tag.rsplit(":", 1)[0],
        "broker_index_digest": (published[broker_tag][0].rsplit("@", 1)[1] if registry_mode else None),
        "broker_platform_manifest_digest": (published[broker_tag][1] if registry_mode else None),
        "broker_config_digest": broker.get("Id"),
        "broker_build_metadata_digest": (published[broker_tag][3] if registry_mode else None),
        "broker_provenance_digest": (published[broker_tag][4] if registry_mode else None),
        "broker_sbom_digest": published[broker_tag][5] if registry_mode else None,
        "broker_code_digest": broker_code_digest,
        "debian_snapshot": DEBIAN_SNAPSHOT,
        "debian_security_snapshot": DEBIAN_SECURITY_SNAPSHOT,
        "browser_dpkg_lock_digest": BROWSER_DPKG_LOCK_DIGEST,
        "browser_dpkg_lock_entries": int(BROWSER_DPKG_LOCK_ENTRIES),
        "broker_dpkg_lock_digest": BROKER_DPKG_LOCK_DIGEST,
        "broker_dpkg_lock_entries": int(BROKER_DPKG_LOCK_ENTRIES),
        "image_build_hermetic": False,
        "image_build_reproducible": False,
        "cryptography_version": CRYPTOGRAPHY_VERSION,
        "image_provenance": ("ghcr_buildkit_max_sbom" if registry_mode else "local_nonclaim"),
        "non_root_defaults": True,
    }
    if native_browser is not None:
        evidence.update(
            {
                "native_browser_tag": NATIVE_BROWSER_TAG,
                "native_browser_config_digest": native_browser.get("Id"),
                "native_browser_version": NATIVE_CHROMIUM_VERSION,
                "native_browser_security_update_lag_ms": None,
            }
        )
    if any(
        key.endswith("_config_digest") and (type(value) is not str or not value.startswith("sha256:"))
        for key, value in evidence.items()
    ):
        raise BuildRejected("image_config_digest")
    return evidence


def build_registry_images(
    *,
    environment: Mapping[str, str],
    qualification_source_digest: str,
    context_archive: bytes,
) -> dict[str, Any]:
    """Build the hosted public-route pair with registry provenance and SBOMs."""

    return build_images(
        hosted_environment=environment,
        qualification_source_digest=qualification_source_digest,
        context_archive=context_archive,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-unverified-native-browser",
        action="store_true",
        help="also build the arm64 Chromium image, which is not public-route eligible",
    )
    args = parser.parse_args(argv)
    try:
        evidence = build_images(include_unverified_native_browser=args.include_unverified_native_browser)
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
