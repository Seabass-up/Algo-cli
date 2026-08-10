from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tarfile

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Boron image evidence uses Linux container and POSIX pipe semantics",
)


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / "algo_cli/resources/boron_browser"


def _build_module():
    path = ROOT / "scripts/boron_browser_build_images.py"
    spec = importlib.util.spec_from_file_location("boron_browser_build_images", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _hosted_context(module) -> tuple[bytes, str]:
    payloads = tuple((relative, (ROOT / relative).read_bytes()) for relative in module.HOSTED_BUILD_CONTEXT_PATHS)
    digest = hashlib.sha256()
    for relative, payload in sorted(payloads):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return (
        module.canonical_build_context_archive(payloads),
        "sha256:" + digest.hexdigest(),
    )


def _version_payload(
    *,
    extra: dict[str, object] | None = None,
    next_page_token: str = "879489531",
) -> bytes:
    document: dict[str, object] = {
        "versions": [
            {
                "name": ("chrome/platforms/linux/channels/stable/versions/151.0.7922.108"),
                "version": "151.0.7922.108",
            }
        ],
        "nextPageToken": next_page_token,
    }
    if extra:
        document.update(extra)
    return json.dumps(document, separators=(",", ":")).encode("ascii")


def _release_payload(
    *,
    fraction: object = 1,
    start_time: object = "2026-08-06T20:04:27.459919Z",
) -> bytes:
    return json.dumps(
        {
            "releases": [
                {
                    "name": ("chrome/platforms/linux/channels/stable/versions/151.0.7922.108/releases/1786046667"),
                    "serving": {"startTime": start_time},
                    "fraction": fraction,
                    "version": "151.0.7922.108",
                    "fractionGroup": "1",
                    "pinnable": True,
                    "rolloutData": [],
                }
            ],
            "nextPageToken": "",
        },
        separators=(",", ":"),
    ).encode("ascii")


def _hosted_environment(**changes: str) -> dict[str, str]:
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "Seabass-up/Algo-cli",
        "GITHUB_REPOSITORY_ID": "1297752684",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "987654321",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_REF": ("Seabass-up/Algo-cli/.github/workflows/oliver-ci.yml@refs/heads/main"),
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "RUNNER_ARCH": "X64",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
    }
    environment.update(changes)
    return environment


def _slsa_predicate() -> dict[str, object]:
    return {
        "buildType": "https://mobyproject.org/buildkit@v1",
        "builder": {"id": "https://github.com/Seabass-up/Algo-cli/actions/runs/987654321/attempts/2"},
        "invocation": {
            "configSource": {"entryPoint": "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile"},
            "parameters": {
                "frontend": "dockerfile.v0",
                "locals": [{"name": "context"}, {"name": "dockerfile"}],
                "args": {"build-arg:BORON_CODE_DIGEST": "sha256:" + "a" * 64},
            },
            "environment": {"platform": "linux/amd64"},
        },
        "metadata": {
            "buildInvocationID": "build-123",
            "buildStartedOn": "2026-08-09T12:00:00Z",
            "buildFinishedOn": "2026-08-09T12:01:00Z",
            "reproducible": False,
            "completeness": {
                "parameters": True,
                "environment": True,
                "materials": False,
            },
        },
        "buildConfig": {"llbDefinition": {"digest": "sha256:" + "b" * 64}},
        "materials": [
            {
                "uri": (
                    "pkg:docker/docker/dockerfile@1.26.0?"
                    "digest=sha256%3Aecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32"
                    "&platform=linux%2Famd64"
                ),
                "digest": {"sha256": "34b128e419449565adc5ed7f487a6f503a73f1077012cfed86354c731338c44f"},
            },
            {
                "uri": (
                    "pkg:docker/docker/buildkit-syft-scanner@1.11.0?"
                    "digest=sha256%3A79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
                    "&platform=linux%2Famd64"
                ),
                "digest": {"sha256": "79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"},
            },
            {
                "uri": (
                    "pkg:docker/debian@bookworm-slim?"
                    "digest=sha256%3A63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"
                    "&platform=linux%2Famd64"
                ),
                "digest": {"sha256": "63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"},
            },
        ],
    }


def _spdx_document(*, role: str = "browser") -> dict[str, object]:
    components = [
        (
            "SPDXRef-Package-cryptography",
            "cryptography",
            "50.0.0",
            "pkg:pypi/cryptography@50.0.0",
        ),
        ("SPDXRef-Package-cffi", "cffi", "2.1.0", "pkg:pypi/cffi@2.1.0"),
        (
            "SPDXRef-Package-pycparser",
            "pycparser",
            "3.0",
            "pkg:pypi/pycparser@3.0",
        ),
    ]
    if role == "browser":
        components.insert(
            0,
            (
                "SPDXRef-Package-google-chrome-stable",
                "google-chrome-stable",
                "151.0.7922.108-1",
                "pkg:deb/debian/google-chrome-stable@151.0.7922.108-1?arch=amd64&distro=debian-12",
            ),
        )
    elif role != "broker":
        raise ValueError(role)
    root_id = "SPDXRef-DocumentRoot-Directory-sbom"
    return {
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "sbom",
        "dataLicense": "CC0-1.0",
        "spdxVersion": "SPDX-2.3",
        "documentNamespace": "https://algo-cli.example/sbom/fixture",
        "creationInfo": {
            "created": "2026-08-09T12:01:00Z",
            "creators": ["Tool: syft-v1.42.3", "Tool: buildkit-v0.32.2"],
        },
        "packages": [
            *[
                {
                    "SPDXID": spdx_id,
                    "name": name,
                    "versionInfo": version,
                    "externalRefs": [
                        {
                            "referenceCategory": "PACKAGE-MANAGER",
                            "referenceType": "purl",
                            "referenceLocator": purl,
                        }
                    ],
                }
                for spdx_id, name, version, purl in components
            ],
            {
                "SPDXID": root_id,
                "name": "sbom",
                "primaryPackagePurpose": "FILE",
            },
        ],
        "files": [],
        "relationships": [
            *[
                {
                    "spdxElementId": root_id,
                    "relatedSpdxElement": spdx_id,
                    "relationshipType": "CONTAINS",
                }
                for spdx_id, _name, _version, _purl in components
            ],
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relatedSpdxElement": root_id,
                "relationshipType": "DESCRIBES",
            },
        ],
    }


def _build_metadata_document() -> dict[str, object]:
    manifest_digest = "sha256:" + "1" * 64
    config_digest = "sha256:" + "2" * 64
    return {
        "containerimage.digest": manifest_digest,
        "containerimage.config.digest": config_digest,
        "containerimage.descriptor": {
            "digest": manifest_digest,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "size": 4_096,
            "annotations": {"config.digest": config_digest},
        },
        "buildx.build.provenance": _slsa_predicate(),
    }


def test_registry_tags_require_exact_protected_main_hosted_authority() -> None:
    module = _build_module()
    suffix = ":run-987654321-2-" + "a" * 40
    assert module.hosted_registry_tags(_hosted_environment()) == (
        "ghcr.io/seabass-up/algo-cli-boron-browser" + suffix,
        "ghcr.io/seabass-up/algo-cli-xenon-broker" + suffix,
    )

    for environment, reason in (
        (_hosted_environment(GITHUB_EVENT_NAME="pull_request"), "registry_environment"),
        (_hosted_environment(GITHUB_REPOSITORY="attacker/repo"), "registry_environment"),
        (_hosted_environment(GITHUB_REPOSITORY_ID="123456789"), "registry_environment"),
        (_hosted_environment(GITHUB_REF="refs/heads/develop"), "registry_environment"),
        (_hosted_environment(GITHUB_REF_PROTECTED="false"), "registry_environment"),
        (_hosted_environment(RUNNER_ENVIRONMENT="self-hosted"), "registry_environment"),
        (_hosted_environment(GITHUB_WORKFLOW_REF="attacker/workflow"), "registry_environment"),
        (_hosted_environment(GITHUB_WORKFLOW_SHA="b" * 40), "registry_identity"),
        (_hosted_environment(GITHUB_SHA="main"), "registry_identity"),
        (_hosted_environment(GITHUB_RUN_ID="0"), "registry_identity"),
    ):
        with pytest.raises(module.BuildRejected, match=reason):
            module.hosted_registry_tags(environment)


def test_dockerfiles_pin_base_downloads_users_and_narrow_copy_surface() -> None:
    browser = (RESOURCE / "boron_public_browser.Dockerfile").read_text(encoding="utf-8")
    broker = (RESOURCE / "xenon_egress_broker.Dockerfile").read_text(encoding="utf-8")
    native = (RESOURCE / "carbon_native_browser.Dockerfile").read_text(encoding="utf-8")
    assert browser.splitlines()[:2] == broker.splitlines()[:2]
    debian_base = "debian:bookworm-slim@sha256:63a496b5d3b99214b39f5ed70eb71a61e590a77979c79cbee4faf991f8c0783e"
    for source, role, user in (
        (browser, "managed-browser", "1000:1000"),
        (broker, "egress-broker", "1001:1001"),
    ):
        frontend = "docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32"
        assert source.startswith("# syntax=" + frontend + "\n")
        assert ("FROM --platform=linux/amd64 " + frontend + " AS dockerfile_frontend_pin") in source
        assert source.count(frontend) == 2
        assert (
            "--mount=type=bind,from=dockerfile_frontend_pin,"
            "source=/bin/dockerfile-frontend,target=/tmp/dockerfile-frontend,readonly"
        ) in source
        assert "test -x /tmp/dockerfile-frontend" in source
        assert source.count("FROM --platform=linux/amd64 " + debian_base) == 1
        assert f'com.algo-cli.role="{role}"' in source
        assert f"USER {user}" in source
        assert "sha256sum --check --strict" in source
        assert "--proto '=https' --tlsv1.2" in source
        assert "COPY algo_cli/ " not in source
        assert "COPY . " not in source
        assert ":latest" not in source
        assert "ADD " not in source
        assert "pip install" not in source
        assert "deb.debian.org" not in source
        assert "security.debian.org" not in source
        assert "http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" in source
        assert ("http://snapshot.debian.org/archive/debian-security/${DEBIAN_SECURITY_SNAPSHOT}/") in source
        assert "ARG DEBIAN_SNAPSHOT=20260712T202631Z" in source
        assert "ARG DEBIAN_SECURITY_SNAPSHOT=20260712T194830Z" in source
        assert source.count("Check-Valid-Until: no") == 2
        assert source.count("Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg") == 2
        assert "ca-certificates=${CA_CERTIFICATES_VERSION}" in source
        assert "curl=${CURL_VERSION}" in source
        assert "passwd=${PASSWD_VERSION}" in source
        assert "python3=${PYTHON3_VERSION}" in source
        assert 'test -z "$(dpkg --audit)"' in source
        assert "dpkg-query -W -f='${binary:Package}=${Version}\\n'" in source
        assert 'com.algo-cli.build.hermetic="false"' in source
        assert 'com.algo-cli.build.reproducible="false"' in source
    assert "google-chrome-stable_151.0.7922.108-1_amd64.deb" in browser
    assert 'com.algo-cli.browser.release-at-ms="1786046667459"' in browser
    assert 'com.algo-cli.cryptography.version="50.0.0"' in browser
    assert 'com.algo-cli.cryptography.version="50.0.0"' in broker
    for source in (browser, broker):
        assert "cryptography-50.0.0-" in source
        assert "cffi-2.1.0-" in source
        assert "pycparser-3.0-" in source
    assert "dpkg-query -W -f='${Version}' google-chrome-stable" in browser
    assert "ARG LIBNSS3_TOOLS_VERSION=2:3.87.1-1+deb12u2" in browser
    assert "libnss3-tools=${LIBNSS3_TOOLS_VERSION}" in browser
    assert "ARG DPKG_LOCK_ENTRIES=228" in browser
    assert ("ARG DPKG_LOCK_SHA256=8de022828059888145925f8fc14424eb1f8b9a2d01d5bb24abff9d2d0d60a1d9") in browser
    assert "ARG DPKG_LOCK_ENTRIES=122" in broker
    assert ("ARG DPKG_LOCK_SHA256=945e9057beb01efbdcf89ca6ba002f260eb6bda40f5d535337e7ca7dc6eed640") in broker
    assert "boron_browser_wrapper.py" in browser
    assert "xenon_browser_broker.py" not in browser
    assert "xenon_browser_broker.py" in broker
    assert "boron_browser_wrapper.py" not in broker
    assert "FROM --platform=linux/arm64 debian:bookworm-slim@sha256:" in native
    assert "chromium_150.0.7871.124-1~deb12u1_arm64.deb" in native
    assert "chromium-common_150.0.7871.124-1~deb12u1_arm64.deb" in native
    assert "chromium-sandbox_150.0.7871.124-1~deb12u1_arm64.deb" in native
    assert 'com.algo-cli.browser.family="chromium_stable"' in native
    assert "USER 1000:1000" in native
    assert "--proto '=https' --tlsv1.2" in native
    assert "sha256sum --check --strict" in native
    assert ":latest" not in native and "COPY algo_cli/ " not in native


def test_dockerfile_specific_ignore_files_are_exact_allowlists() -> None:
    browser_allowlist = (
        "**\n"
        "!algo_cli/\n"
        "!algo_cli/__init__.py\n"
        "!algo_cli/boron_browser_entry.py\n"
        "!algo_cli/boron_browser_wrapper.py\n"
        "!algo_cli/resources/\n"
        "!algo_cli/resources/boron_browser/\n"
        "!algo_cli/resources/boron_browser/boron_browser_wrapper.sh\n"
        "!algo_cli/resources/boron_browser/boron_managed_policy.json\n"
    )
    broker_allowlist = (
        "**\n"
        "!algo_cli/\n"
        "!algo_cli/__init__.py\n"
        "!algo_cli/xenon_browser_broker.py\n"
        "!algo_cli/xenon_browser_egress.py\n"
        "!algo_cli/xenon_browser_entry.py\n"
        "!algo_cli/resources/\n"
        "!algo_cli/resources/boron_browser/\n"
        "!algo_cli/resources/boron_browser/xenon_egress_broker.sh\n"
    )
    assert (RESOURCE / "boron_public_browser.Dockerfile.dockerignore").read_text(encoding="utf-8") == browser_allowlist
    assert (RESOURCE / "carbon_native_browser.Dockerfile.dockerignore").read_text(encoding="utf-8") == browser_allowlist
    assert (RESOURCE / "xenon_egress_broker.Dockerfile.dockerignore").read_text(encoding="utf-8") == broker_allowlist


def test_spdx_semantics_are_role_specific_and_relationship_bound() -> None:
    module = _build_module()
    browser_dockerfile = "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile"
    broker_dockerfile = "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile"

    for dockerfile, role in ((browser_dockerfile, "browser"), (broker_dockerfile, "broker")):
        module._validate_spdx_document(
            _spdx_document(role=role),
            stage=role + "_sbom",
            expected_components=module._SBOM_COMPONENTS_BY_DOCKERFILE[dockerfile],
            forbidden_component_names=module._SBOM_FORBIDDEN_COMPONENTS_BY_DOCKERFILE[dockerfile],
        )

    with pytest.raises(module.BuildRejected, match="broker_sbom_components"):
        module._validate_spdx_document(
            _spdx_document(role="browser"),
            stage="broker_sbom",
            expected_components=module._SBOM_COMPONENTS_BY_DOCKERFILE[broker_dockerfile],
            forbidden_component_names=module._SBOM_FORBIDDEN_COMPONENTS_BY_DOCKERFILE[broker_dockerfile],
        )
    with pytest.raises(module.BuildRejected, match="browser_sbom_components"):
        module._validate_spdx_document(
            _spdx_document(role="broker"),
            stage="browser_sbom",
            expected_components=module._SBOM_COMPONENTS_BY_DOCKERFILE[browser_dockerfile],
            forbidden_component_names=module._SBOM_FORBIDDEN_COMPONENTS_BY_DOCKERFILE[browser_dockerfile],
        )


def test_launchers_are_fixed_isolated_python_modules() -> None:
    assert (RESOURCE / "boron_browser_wrapper.sh").read_text(encoding="utf-8") == (
        "#!/bin/sh\nset -eu\n\nexec /usr/bin/python3 -B -I -u -m algo_cli.boron_browser_entry\n"
    )
    assert (RESOURCE / "xenon_egress_broker.sh").read_text(encoding="utf-8") == (
        "#!/bin/sh\nset -eu\n\nexec /usr/bin/python3 -B -I -u -m algo_cli.xenon_browser_entry\n"
    )


def test_code_digest_binds_names_lengths_and_contents(tmp_path: Path) -> None:
    module = _build_module()
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    module.ROOT = tmp_path
    digest = module._tree_digest((second, first))
    expected = hashlib.sha256()
    for name in (b"a", b"b"):
        expected.update(len(name).to_bytes(4, "big"))
        expected.update(name)
        expected.update((4).to_bytes(8, "big"))
        expected.update(b"same")
    assert digest == "sha256:" + expected.hexdigest()
    with pytest.raises(module.BuildRejected, match="code_file_missing"):
        module._tree_digest((tmp_path / "missing",))


def test_run_forwards_the_exact_binary_build_context_to_stdin(monkeypatch) -> None:
    module = _build_module()
    payload = b"ustar\x00context\xffbytes"
    observed: dict[str, object] = {}

    def run(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        return module.subprocess.CompletedProcess(args, 0, b"stdout", b"stderr")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module._run(
        ["docker", "buildx", "build", "-"],
        stage="binary_context",
        input_bytes=payload,
    )
    assert observed["input"] is payload
    assert observed["text"] is False
    assert "encoding" not in observed
    assert hashlib.sha256(observed["input"]).digest() == hashlib.sha256(payload).digest()
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"


def test_version_history_parser_is_strict_and_exact() -> None:
    module = _build_module()
    version = module._parse_latest_version(_version_payload())
    assert version == module.CHROME_VERSION
    assert module._parse_latest_release(_release_payload(), version=version) == module.CHROME_RELEASE_AT_MS
    with pytest.raises(module.BuildRejected, match="release_evidence_duplicate_key"):
        module._strict_json(b'{"versions":[],"versions":[]}')
    with pytest.raises(module.BuildRejected, match="release_evidence_version_shape"):
        module._parse_latest_version(_version_payload(extra={"unexpected": True}))
    with pytest.raises(module.BuildRejected, match="release_evidence_release_state"):
        module._parse_latest_release(_release_payload(fraction=0), version=version)
    with pytest.raises(module.BuildRejected, match="release_evidence_time"):
        module._parse_latest_release(
            _release_payload(start_time="2026-07-16 20:53:47Z"),
            version=version,
        )


def test_version_history_fetch_builds_digest_bound_evidence(monkeypatch) -> None:
    module = _build_module()
    requested: list[str] = []

    def fake_fetch(url: str) -> bytes:
        requested.append(url)
        return _version_payload() if url == module.VERSION_HISTORY_URL else _release_payload()

    monkeypatch.setattr(module, "_fetch_json_bytes", fake_fetch)
    evidence = module.fetch_browser_release_evidence(observed_at_ms=module.CHROME_RELEASE_AT_MS + 1_000)
    assert requested == [
        module.VERSION_HISTORY_URL,
        (module.VERSION_RELEASE_URL_PREFIX + module.CHROME_VERSION + "/releases?pageSize=1"),
    ]
    assert evidence.browser_version == module.CHROME_VERSION
    assert evidence.security_release_at_ms == module.CHROME_RELEASE_AT_MS
    assert evidence.source_digest.startswith("sha256:")
    assert evidence.source_digest == module._release_source_digest(
        version=module.CHROME_VERSION,
        release_at_ms=module.CHROME_RELEASE_AT_MS,
    )
    assert (
        module._parse_latest_version(_version_payload(next_page_token="different-opaque-token"))
        == module.CHROME_VERSION
    )


def test_version_history_transport_rejects_redirects_and_oversized_bodies(
    monkeypatch,
) -> None:
    module = _build_module()

    class Headers:
        def __init__(self, values: dict[str, str]) -> None:
            self.values = values

        def get_content_type(self) -> str:
            return "application/json"

        def get(self, key: str, default: str | None = None) -> str | None:
            return self.values.get(key, default)

    class Response:
        status = 200

        def __init__(self, *, final_url: str, length: int) -> None:
            self.final_url = final_url
            self.headers = Headers({"Content-Length": str(length)})

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def geturl(self) -> str:
            return self.final_url

        def read(self, _limit: int) -> bytes:
            return _version_payload()

    class Opener:
        def __init__(self, response: Response) -> None:
            self.response = response

        def open(self, *_args, **_kwargs) -> Response:
            return self.response

    monkeypatch.setattr(
        module.urllib.request,
        "build_opener",
        lambda *_args: Opener(
            Response(
                final_url="https://example.invalid/redirected",
                length=len(_version_payload()),
            )
        ),
    )
    with pytest.raises(module.BuildRejected, match="release_evidence_response"):
        module._fetch_json_bytes(module.VERSION_HISTORY_URL)

    monkeypatch.setattr(
        module.urllib.request,
        "build_opener",
        lambda *_args: Opener(
            Response(
                final_url=module.VERSION_HISTORY_URL,
                length=module.MAX_RELEASE_RESPONSE_BYTES + 1,
            )
        ),
    )
    with pytest.raises(module.BuildRejected, match="release_evidence_size"):
        module._fetch_json_bytes(module.VERSION_HISTORY_URL)
    with pytest.raises(module.BuildRejected, match="release_evidence_url"):
        module._fetch_json_bytes("https://example.invalid/releases")


def test_build_update_lag_gate_rejects_before_docker(monkeypatch) -> None:
    module = _build_module()
    called: list[object] = []

    def rejected_command(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("Docker must not run after stale rejection")

    monkeypatch.setattr(module, "_run", rejected_command)
    now_ms = module.CHROME_RELEASE_AT_MS + 10 * 86_400_000
    release_evidence = module.BoronBrowserReleaseEvidence(
        module.BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY,
        module.BoronBrowserFamily.CHROME_STABLE,
        "151.0.7922.109",
        module.PLATFORM,
        now_ms - module.BORON_MAX_SECURITY_LAG_MS - 1,
        now_ms,
        "sha256:" + "9" * 64,
    )
    with pytest.raises(module.BuildRejected, match="browser_security_update_stale"):
        module.build_images(
            now_ms=now_ms,
            release_evidence=release_evidence,
        )
    assert called == []


def test_current_release_age_does_not_block_docker_probe(monkeypatch) -> None:
    module = _build_module()
    called: list[object] = []
    now_ms = module.CHROME_RELEASE_AT_MS + 30 * 86_400_000
    release_evidence = module.BoronBrowserReleaseEvidence(
        module.BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY,
        module.BoronBrowserFamily.CHROME_STABLE,
        module.CHROME_VERSION,
        module.PLATFORM,
        module.CHROME_RELEASE_AT_MS,
        now_ms,
        "sha256:" + "a" * 64,
    )

    def docker_probe(*_args, **_kwargs):
        called.append(True)
        raise module.BuildRejected("docker_probe_reached")

    monkeypatch.setattr(module, "_run", docker_probe)
    with pytest.raises(module.BuildRejected, match="docker_probe_reached"):
        module.build_images(now_ms=now_ms, release_evidence=release_evidence)
    assert called == [True]


def test_build_metadata_reader_is_bounded_regular_and_content_digest_bound(
    tmp_path: Path,
) -> None:
    module = _build_module()
    document = _build_metadata_document()
    payload = json.dumps(document, separators=(",", ":")).encode("ascii")
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(payload)

    assert module._strict_build_metadata(metadata) == (
        document,
        "sha256:" + hashlib.sha256(payload).hexdigest(),
    )

    linked = tmp_path / "linked.json"
    linked.symlink_to(metadata)
    with pytest.raises(module.BuildRejected, match="build_metadata_file"):
        module._strict_build_metadata(linked)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"containerimage.digest":"a","containerimage.digest":"b"}')
    with pytest.raises(module.BuildRejected):
        module._strict_build_metadata(duplicate)

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(module.BuildRejected, match="build_metadata_file"):
        module._strict_build_metadata(empty)


def test_validated_build_metadata_requires_oci_index_config_and_real_provenance(
    tmp_path: Path,
) -> None:
    module = _build_module()
    metadata = tmp_path / "metadata.json"

    def install(document: dict[str, object]) -> None:
        metadata.write_text(json.dumps(document), encoding="utf-8")

    document = _build_metadata_document()
    install(document)
    observed, digest, manifest_digest, config_digest = module._validated_build_metadata(metadata)
    assert observed == document
    assert digest.startswith("sha256:")
    assert manifest_digest == "sha256:" + "1" * 64
    assert config_digest == "sha256:" + "2" * 64

    adversarial: list[tuple[dict[str, object], str]] = []
    wrong = deepcopy(document)
    wrong["containerimage.descriptor"]["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
    adversarial.append((wrong, "build_metadata_identity"))
    wrong = deepcopy(document)
    wrong["containerimage.descriptor"]["size"] = 0
    adversarial.append((wrong, "build_metadata_identity"))
    wrong = deepcopy(document)
    wrong["containerimage.descriptor"]["annotations"]["config.digest"] = "sha256:" + "3" * 64
    adversarial.append((wrong, "build_metadata_identity"))
    wrong = deepcopy(document)
    wrong["buildx.build.provenance"] = {"garbage": "previously accepted"}
    adversarial.append((wrong, "build_metadata_provenance_shape"))
    wrong = deepcopy(document)
    wrong["buildx.build.provenance"]["metadata"]["reproducible"] = True
    adversarial.append((wrong, "build_metadata_provenance_metadata"))

    for malformed, reason in adversarial:
        install(malformed)
        with pytest.raises(module.BuildRejected, match=reason):
            module._validated_build_metadata(metadata)


def test_registry_index_resolves_raw_attestation_bound_amd64_descriptors(
    monkeypatch,
) -> None:
    module = _build_module()
    platform_digest = "sha256:" + "2" * 64
    attestation_digest = "sha256:" + "3" * 64
    document = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "digest": platform_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 1_024,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": attestation_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 2_048,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": platform_digest,
                },
            },
        ],
    }
    commands: list[tuple[list[str], dict[str, object]]] = []

    def invoke(candidate: dict[str, object]):
        payload = json.dumps(candidate, separators=(",", ":")).encode("ascii")
        index_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        reference = "ghcr.io/seabass-up/algo-cli-boron-browser@" + index_digest

        def fake_run(args, **kwargs):
            commands.append((args, kwargs))
            return module.subprocess.CompletedProcess(args, 0, payload.decode(), "")

        monkeypatch.setattr(module, "_run", fake_run)
        return module._registry_index_descriptors(
            reference,
            platform="linux/amd64",
            expected_size=len(payload),
            stage="browser_index",
        ), reference

    observed, reference = invoke(document)
    assert observed == (platform_digest, 1_024, attestation_digest, 2_048)
    assert commands == [
        (
            ["docker", "buildx", "imagetools", "inspect", reference, "--raw"],
            {"stage": "browser_index", "timeout": 120},
        )
    ]

    adversarial = []
    wrong = deepcopy(document)
    wrong["manifests"][0]["mediaType"] = "application/vnd.oci.image.index.v1+json"
    adversarial.append((wrong, "browser_index_shape"))
    wrong = deepcopy(document)
    wrong["manifests"][1]["size"] = 0
    adversarial.append((wrong, "browser_index_shape"))
    wrong = deepcopy(document)
    wrong["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = "sha256:" + "5" * 64
    adversarial.append((wrong, "browser_index_attestation_binding"))
    wrong = deepcopy(document)
    wrong["manifests"].append(deepcopy(document["manifests"][1]))
    adversarial.append((wrong, "browser_index_shape"))
    for malformed, reason in adversarial:
        with pytest.raises(module.BuildRejected, match=reason):
            invoke(malformed)


def test_raw_registry_attestations_bind_envelopes_subjects_and_pinned_materials(
    monkeypatch,
) -> None:
    module = _build_module()
    tag = "ghcr.io/seabass-up/algo-cli-boron-browser:run-987654321-2-" + "a" * 40
    platform_digest = "sha256:" + "2" * 64
    platform_size = 1_024
    expected_builder = "https://github.com/Seabass-up/Algo-cli/actions/runs/987654321/attempts/2"
    expected_dockerfile = "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile"
    expected_parameters = {"build-arg:BORON_CODE_DIGEST": "sha256:" + "a" * 64}
    subject = [
        {
            "name": module._registry_subject_name(tag, platform="linux/amd64"),
            "digest": {"sha256": "2" * 64},
        }
    ]

    def statement(predicate_type: str, predicate: dict[str, object]):
        return {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": deepcopy(subject),
            "predicateType": predicate_type,
            "predicate": predicate,
        }

    provenance = statement(module._SLSA_PREDICATE_TYPE, _slsa_predicate())
    sbom = statement(module._SPDX_PREDICATE_TYPE, _spdx_document())
    manifest_commands: list[tuple[list[str], dict[str, object]]] = []
    blob_calls: list[dict[str, object]] = []
    last_manifest_digest = ""

    def invoke(
        provenance_statement: dict[str, object],
        sbom_statement: dict[str, object],
        *,
        layer_predicates: tuple[str, ...] | None = None,
        manifest_platform: dict[str, str] | None = None,
    ):
        nonlocal last_manifest_digest
        predicates = layer_predicates or (
            module._SLSA_PREDICATE_TYPE,
            module._SPDX_PREDICATE_TYPE,
        )
        payloads = [
            json.dumps(
                provenance_statement if predicate_type == module._SLSA_PREDICATE_TYPE else sbom_statement,
                separators=(",", ":"),
            ).encode("ascii")
            for predicate_type in predicates
        ]
        layers = [
            {
                "mediaType": "application/vnd.in-toto+json",
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "annotations": {"in-toto.io/predicate-type": predicate_type},
            }
            for payload, predicate_type in zip(payloads, predicates, strict=True)
        ]
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": module._EMPTY_JSON_DIGEST,
                "size": 2,
                "data": "e30=",
            },
            "layers": layers,
            "subject": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": platform_digest,
                "size": platform_size,
            },
        }
        if manifest_platform is not None:
            manifest["subject"]["platform"] = manifest_platform
        manifest_payload = json.dumps(manifest, separators=(",", ":")).encode("ascii")
        manifest_digest = "sha256:" + hashlib.sha256(manifest_payload).hexdigest()
        last_manifest_digest = manifest_digest
        by_digest = {layer["digest"]: payload for layer, payload in zip(layers, payloads, strict=True)}

        def raw_manifest(args, **kwargs):
            manifest_commands.append((args, kwargs))
            return module.subprocess.CompletedProcess(
                args,
                0,
                manifest_payload.decode(),
                "",
            )

        def raw_blob(repository, *, digest, expected_size, stage):
            blob_calls.append(
                {
                    "repository": repository,
                    "digest": digest,
                    "expected_size": expected_size,
                    "stage": stage,
                }
            )
            return by_digest[digest]

        monkeypatch.setattr(
            module,
            "_run",
            raw_manifest,
        )
        monkeypatch.setattr(
            module,
            "_registry_blob_bytes",
            raw_blob,
        )
        return module._registry_attestation_digests(
            tag=tag,
            platform_manifest_digest=platform_digest,
            platform_manifest_size=platform_size,
            attestation_manifest_digest=manifest_digest,
            attestation_manifest_size=len(manifest_payload),
            stage="browser_attestations",
            expected_builder_id=expected_builder,
            expected_platform="linux/amd64",
            expected_dockerfile=expected_dockerfile,
            expected_parameters=expected_parameters,
        )

    observed = invoke(provenance, sbom)
    assert observed == (
        "sha256:" + hashlib.sha256(json.dumps(provenance, separators=(",", ":")).encode("ascii")).hexdigest(),
        "sha256:" + hashlib.sha256(json.dumps(sbom, separators=(",", ":")).encode("ascii")).hexdigest(),
    )
    assert manifest_commands[-1] == (
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "ghcr.io/seabass-up/algo-cli-boron-browser@" + last_manifest_digest,
            "--raw",
        ],
        {"stage": "browser_attestations_manifest", "timeout": 120},
    )
    assert [call["digest"] for call in blob_calls[-2:]] == list(observed)
    assert all(call["repository"] == "seabass-up/algo-cli-boron-browser" for call in blob_calls[-2:])
    assert (
        invoke(
            provenance,
            sbom,
            manifest_platform={"architecture": "amd64", "os": "linux"},
        )
        == observed
    )
    with pytest.raises(module.BuildRejected, match="browser_attestations_manifest_shape"):
        invoke(
            provenance,
            sbom,
            manifest_platform={"architecture": "arm64", "os": "linux"},
        )

    adversarial: list[tuple[dict[str, object], dict[str, object], tuple[str, ...] | None, str]] = []
    wrong = deepcopy(provenance)
    wrong["_type"] = "https://in-toto.io/Statement/v0.1"
    adversarial.append((wrong, sbom, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(provenance)
    wrong["subject"][0]["name"] = "pkg:docker/attacker/image@latest"
    adversarial.append((wrong, sbom, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(provenance)
    wrong["subject"] = []
    adversarial.append((wrong, sbom, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(provenance)
    wrong["subject"].append(deepcopy(wrong["subject"][0]))
    adversarial.append((wrong, sbom, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(sbom)
    wrong["subject"][0]["digest"]["sha256"] = "9" * 64
    adversarial.append((provenance, wrong, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(provenance)
    wrong["predicateType"] = module._SPDX_PREDICATE_TYPE
    adversarial.append((wrong, sbom, None, "browser_attestations_statement_binding"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"][0]["digest"]["sha256"] = "9" * 64
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"][1]["digest"]["sha256"] = "9" * 64
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"][2]["digest"]["sha256"] = "9" * 64
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"][2] = {
        "uri": "pkg:docker/debian@latest?platform=linux%2Famd64",
        "digest": {"sha256": "8" * 64},
    }
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"].append(
        {
            "uri": "pkg:docker/debian@latest?platform=linux%2Famd64",
            "digest": {"sha256": "8" * 64},
        }
    )
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"].append(
        {
            "uri": "pkg:docker/docker/dockerfile@latest?platform=linux%2Famd64",
            "digest": {"sha256": "8" * 64},
        }
    )
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"].append(
        {
            "uri": "pkg:docker/docker/%64ockerfile@latest?platform=linux%2Famd64",
            "digest": {"sha256": "8" * 64},
        }
    )
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"].append(
        {
            "uri": ("pkg:docker/docker/buildkit-syft-scanner@stable-1?platform=linux%2Famd64"),
            "digest": {"sha256": "8" * 64},
        }
    )
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["materials"].append(
        {
            "uri": "https://attacker.example/source.tar.gz",
            "digest": {"sha256": "8" * 64},
        }
    )
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_materials"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["metadata"]["completeness"]["materials"] = True
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_metadata"))
    wrong = deepcopy(provenance)
    wrong["predicate"]["invocation"]["parameters"]["args"]["build-arg:UNEXPECTED"] = "1"
    adversarial.append((wrong, sbom, None, "browser_attestations_provenance_parameters"))
    wrong = deepcopy(sbom)
    wrong["predicate"]["spdxVersion"] = "SPDX-1.0"
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_shape"))
    wrong = deepcopy(sbom)
    root_package = deepcopy(wrong["predicate"]["packages"][-1])
    wrong["predicate"]["packages"] = [root_package]
    wrong["predicate"]["relationships"] = [deepcopy(wrong["predicate"]["relationships"][-1])]
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_components"))
    wrong = deepcopy(sbom)
    wrong["predicate"]["packages"][0]["versionInfo"] = "151.0.7922.109-1"
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_components"))
    wrong = deepcopy(sbom)
    wrong["predicate"]["packages"][0]["externalRefs"][0]["referenceLocator"] = (
        "pkg:deb/debian/google-chrome-stable@151.0.7922.109-1?arch=amd64&distro=debian-12"
    )
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_components"))
    wrong = deepcopy(sbom)
    wrong["predicate"]["relationships"].pop(0)
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_relationships"))
    wrong = deepcopy(sbom)
    wrong["predicate"]["relationships"].append(
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relatedSpdxElement": "SPDXRef-Package-google-chrome-stable",
            "relationshipType": "DESCRIBES",
        }
    )
    adversarial.append((provenance, wrong, None, "browser_attestations_sbom_relationships"))
    adversarial.append(
        (
            provenance,
            sbom,
            (module._SLSA_PREDICATE_TYPE, module._SLSA_PREDICATE_TYPE),
            "browser_attestations_attestation_count",
        )
    )
    adversarial.append(
        (
            provenance,
            sbom,
            (module._SLSA_PREDICATE_TYPE,),
            "browser_attestations_manifest_shape",
        )
    )
    adversarial.append(
        (
            provenance,
            sbom,
            (
                module._SLSA_PREDICATE_TYPE,
                module._SPDX_PREDICATE_TYPE,
                module._SPDX_PREDICATE_TYPE,
            ),
            "browser_attestations_manifest_shape",
        )
    )
    for provenance_row, sbom_row, predicates, reason in adversarial:
        with pytest.raises(module.BuildRejected, match=reason):
            invoke(
                provenance_row,
                sbom_row,
                layer_predicates=predicates,
            )


def test_published_build_uses_bound_builder_provenance_sbom_and_exact_digest(
    monkeypatch,
) -> None:
    module = _build_module()
    context_archive, source_digest = _hosted_context(module)
    dockerfile = "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile"
    tag = "ghcr.io/seabass-up/algo-cli-boron-browser:run-987654321-2-" + "a" * 40
    index_digest = "sha256:" + "1" * 64
    platform_digest = "sha256:" + "2" * 64
    config_digest = "sha256:" + "3" * 64
    metadata_digest = "sha256:" + "4" * 64
    provenance_digest = "sha256:" + "5" * 64
    sbom_digest = "sha256:" + "6" * 64
    attestation_manifest_digest = "sha256:" + "7" * 64
    build_arg = "BORON_CODE_DIGEST=sha256:" + "a" * 64
    builder_id = "https://github.com/Seabass-up/Algo-cli/actions/runs/987654321/attempts/2"
    labels = (
        "org.opencontainers.image.source=https://github.com/Seabass-up/Algo-cli",
        "org.opencontainers.image.revision=" + "a" * 40,
        "com.algo-cli.github.repository-id=1297752684",
        "com.algo-cli.github.run-id=987654321",
        "com.algo-cli.github.run-attempt=2",
        "com.algo-cli.qualification.source.sha256=" + source_digest,
    )
    reference = tag.rsplit(":", 1)[0] + "@" + index_digest
    commands: list[tuple[list[str], str, dict[str, str] | None, Path | None, bytes | None]] = []

    def fake_run(
        args,
        *,
        stage="command",
        timeout=900,
        environment=None,
        working_directory=None,
        input_bytes=None,
    ):
        commands.append((args, stage, environment, working_directory, input_bytes))
        if stage == "browser_build":
            metadata_path = Path(args[args.index("--metadata-file") + 1])
            metadata_path.write_text(
                json.dumps(
                    {
                        "containerimage.digest": index_digest,
                        "containerimage.config.digest": config_digest,
                        "containerimage.descriptor": {
                            "digest": index_digest,
                            "mediaType": "application/vnd.oci.image.index.v1+json",
                            "size": 4_096,
                            "annotations": {"config.digest": config_digest},
                        },
                        "buildx.build.provenance": _slsa_predicate(),
                    }
                ),
                encoding="utf-8",
            )
        return module.subprocess.CompletedProcess(args, 0, "", "")

    attestation_calls: list[dict[str, object]] = []

    def fake_attestation(**kwargs) -> tuple[str, str]:
        attestation_calls.append(kwargs)
        return provenance_digest, sbom_digest

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(
        module,
        "_registry_index_descriptors",
        lambda *_args, **_kwargs: (
            platform_digest,
            1_024,
            attestation_manifest_digest,
            2_048,
        ),
    )
    monkeypatch.setattr(
        module,
        "_inspect",
        lambda observed: {
            "Id": config_digest,
            "RepoDigests": [observed],
        },
    )
    monkeypatch.setattr(module, "_registry_attestation_digests", fake_attestation)
    monkeypatch.setattr(
        module,
        "_strict_build_metadata",
        lambda path: (
            json.loads(path.read_text(encoding="utf-8")),
            metadata_digest,
        ),
    )

    assert module._published_build(
        context_archive=context_archive,
        qualification_source_digest=source_digest,
        dockerfile=dockerfile,
        tag=tag,
        build_arg=build_arg,
        stage="browser_build",
        platform="linux/amd64",
        labels=labels,
        builder_id=builder_id,
    ) == (
        reference,
        platform_digest,
        config_digest,
        metadata_digest,
        provenance_digest,
        sbom_digest,
    )
    (
        build_command,
        build_stage,
        build_environment,
        build_directory,
        build_input,
    ) = commands[0]
    assert build_stage == "browser_build"
    assert build_environment == {
        "BUILDX_GIT_INFO": "true",
        "BUILDX_GIT_LABELS": "full",
        "BUILDX_METADATA_PROVENANCE": "max",
    }
    assert build_directory is None
    assert build_input is context_archive
    assert hashlib.sha256(build_input).digest() == hashlib.sha256(context_archive).digest()
    metadata_path = build_command[build_command.index("--metadata-file") + 1]
    assert build_command == [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--pull",
        "--output=type=registry,oci-mediatypes=true,oci-artifact=true",
        "--provenance=mode=max,version=v0.2,builder-id=" + builder_id,
        "--sbom=generator=" + module.SBOM_GENERATOR_REFERENCE,
        "--metadata-file",
        metadata_path,
        "--file",
        "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
        "--build-arg",
        build_arg,
        "--tag",
        tag,
        *[item for label in labels for item in ("--label", label)],
        "-",
    ]
    assert commands[1] == (
        ["docker", "pull", "--platform", "linux/amd64", reference],
        "browser_build_pull",
        None,
        None,
        None,
    )
    assert len(attestation_calls) == 1
    assert attestation_calls[0]["tag"] == tag
    assert attestation_calls[0]["platform_manifest_digest"] == platform_digest
    assert attestation_calls[0]["platform_manifest_size"] == 1_024
    assert attestation_calls[0]["attestation_manifest_digest"] == attestation_manifest_digest
    assert attestation_calls[0]["attestation_manifest_size"] == 2_048
    assert attestation_calls[0]["expected_builder_id"] == builder_id
    assert attestation_calls[0]["expected_platform"] == "linux/amd64"
    assert attestation_calls[0]["expected_dockerfile"] == (
        "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile"
    )
    expected_parameters = attestation_calls[0]["expected_parameters"]
    assert expected_parameters["build-arg:BORON_CODE_DIGEST"] == "sha256:" + "a" * 64
    assert expected_parameters["label:com.algo-cli.github.run-id"] == "987654321"

    with pytest.raises(module.BuildRejected, match="registry_builder_identity"):
        module._published_build(
            context_archive=context_archive,
            qualification_source_digest=source_digest,
            dockerfile=dockerfile,
            tag=tag,
            build_arg=build_arg,
            stage="browser_build",
            platform="linux/amd64",
            labels=labels,
            builder_id="https://github.com/Seabass-up/Algo-cli/actions/runs/1/attempts/1",
        )


def test_hosted_build_requires_the_exact_canonical_archive() -> None:
    module = _build_module()
    context_archive, source_digest = _hosted_context(module)
    assert module._validated_build_context_archive(
        context_archive,
        qualification_source_digest=source_digest,
    ) == {relative: (ROOT / relative).read_bytes() for relative in module.HOSTED_BUILD_CONTEXT_PATHS}
    with pytest.raises(module.BuildRejected, match="registry_build_context"):
        module.build_images(
            hosted_environment=_hosted_environment(),
            qualification_source_digest=source_digest,
        )
    with pytest.raises(module.BuildRejected, match="registry_build_context"):
        module.build_images(
            hosted_environment=_hosted_environment(),
            qualification_source_digest=source_digest,
            context_root=module.ROOT,
            context_archive=context_archive,
        )
    with pytest.raises(module.BuildRejected, match="registry_build_context"):
        module._validated_build_context_archive(
            context_archive,
            qualification_source_digest="sha256:" + "f" * 64,
        )


def test_hosted_build_rejects_oversize_truncated_duplicate_and_link_archives(
    monkeypatch,
) -> None:
    module = _build_module()
    context_archive, source_digest = _hosted_context(module)

    monkeypatch.setattr(module, "MAX_BUILD_CONTEXT_BYTES", len(context_archive) - 1)
    with pytest.raises(module.BuildRejected, match="registry_build_context"):
        module._validated_build_context_archive(
            context_archive,
            qualification_source_digest=source_digest,
        )
    monkeypatch.setattr(module, "MAX_BUILD_CONTEXT_BYTES", 64 * 1024 * 1024)
    with pytest.raises(module.BuildRejected, match="registry_build_context"):
        module._validated_build_context_archive(
            context_archive[:-512],
            qualification_source_digest=source_digest,
        )

    with tarfile.open(fileobj=io.BytesIO(context_archive), mode="r:") as source:
        members = source.getmembers()
        payloads = {member.name: source.extractfile(member).read() for member in members if member.isfile()}

    def rewritten(*, duplicate: bool = False, link: bool = False) -> bytes:
        output = io.BytesIO()
        target = module.HOSTED_BUILD_CONTEXT_PATHS[0]
        with tarfile.open(
            fileobj=output,
            mode="w:",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            for original in members:
                member = deepcopy(original)
                if link and member.name == target:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "pyproject.toml"
                    member.size = 0
                    archive.addfile(member)
                elif member.isfile():
                    archive.addfile(member, io.BytesIO(payloads[member.name]))
                else:
                    archive.addfile(member)
            if duplicate:
                original = next(member for member in members if member.name == target)
                archive.addfile(deepcopy(original), io.BytesIO(payloads[target]))
        return output.getvalue()

    for malformed in (rewritten(duplicate=True), rewritten(link=True)):
        with pytest.raises(module.BuildRejected, match="registry_build_context"):
            module._validated_build_context_archive(
                malformed,
                qualification_source_digest=source_digest,
            )


def test_registry_build_reinspection_rejects_a_changed_local_config(monkeypatch) -> None:
    module = _build_module()
    environment = _hosted_environment()
    context_archive, source_digest = _hosted_context(module)
    context_payloads = module._validated_build_context_archive(
        context_archive,
        qualification_source_digest=source_digest,
    )
    browser_tag, broker_tag = module.hosted_registry_tags(environment)
    browser_reference = browser_tag.rsplit(":", 1)[0] + "@sha256:" + "1" * 64
    broker_reference = broker_tag.rsplit(":", 1)[0] + "@sha256:" + "4" * 64
    browser_code_digest = module._payload_tree_digest(
        module.BROWSER_CODE,
        payloads=context_payloads,
    )
    broker_code_digest = module._payload_tree_digest(
        module.BROKER_CODE,
        payloads=context_payloads,
    )
    source_labels = {
        "org.opencontainers.image.source": "https://github.com/Seabass-up/Algo-cli",
        "org.opencontainers.image.revision": "a" * 40,
        "com.algo-cli.github.repository-id": "1297752684",
        "com.algo-cli.github.run-id": "987654321",
        "com.algo-cli.github.run-attempt": "2",
        "com.algo-cli.qualification.source.sha256": source_digest,
    }
    shared_supply_chain_labels = {
        "com.algo-cli.debian.snapshot": module.DEBIAN_SNAPSHOT,
        "com.algo-cli.debian.security-snapshot": module.DEBIAN_SECURITY_SNAPSHOT,
        "com.algo-cli.build.hermetic": "false",
        "com.algo-cli.build.reproducible": "false",
    }
    published = {
        browser_tag: (
            browser_reference,
            "sha256:" + "6" * 64,
            "sha256:" + "c" * 64,
            "sha256:" + "d" * 64,
            "sha256:" + "7" * 64,
            "sha256:" + "8" * 64,
        ),
        broker_tag: (
            broker_reference,
            "sha256:" + "9" * 64,
            "sha256:" + "e" * 64,
            "sha256:" + "f" * 64,
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
        ),
    }
    published_calls: list[dict[str, object]] = []

    def publish(**kwargs):
        published_calls.append(kwargs)
        return published[kwargs["tag"]]

    monkeypatch.setattr(
        module,
        "_published_build",
        publish,
    )
    monkeypatch.setattr(
        module,
        "_run",
        lambda args, **_kwargs: module.subprocess.CompletedProcess(args, 0, "", ""),
    )

    def inspect(reference: str) -> dict[str, object]:
        if reference == browser_reference:
            return {
                "Architecture": "amd64",
                "Config": {
                    "Labels": {
                        **source_labels,
                        **shared_supply_chain_labels,
                        "com.algo-cli.role": "managed-browser",
                        "com.algo-cli.code.sha256": browser_code_digest,
                        "com.algo-cli.browser.version": module.CHROME_VERSION,
                        "com.algo-cli.browser.release-at-ms": str(module.CHROME_RELEASE_AT_MS),
                        "com.algo-cli.dpkg.lock.sha256": module.BROWSER_DPKG_LOCK_DIGEST,
                        "com.algo-cli.dpkg.lock.entries": module.BROWSER_DPKG_LOCK_ENTRIES,
                    },
                    "User": "1000:1000",
                },
                "Id": "sha256:" + "0" * 64,
                "RepoDigests": [browser_reference],
            }
        assert reference == broker_reference
        return {
            "Architecture": "amd64",
            "Config": {
                "Labels": {
                    **source_labels,
                    **shared_supply_chain_labels,
                    "com.algo-cli.role": "egress-broker",
                    "com.algo-cli.code.sha256": broker_code_digest,
                    "com.algo-cli.dpkg.lock.sha256": module.BROKER_DPKG_LOCK_DIGEST,
                    "com.algo-cli.dpkg.lock.entries": module.BROKER_DPKG_LOCK_ENTRIES,
                },
                "User": "1001:1001",
            },
            "Id": "sha256:" + "e" * 64,
            "RepoDigests": [broker_reference],
        }

    monkeypatch.setattr(module, "_inspect", inspect)
    now_ms = module.CHROME_RELEASE_AT_MS + 60_000
    release_evidence = module.BoronBrowserReleaseEvidence(
        module.BoronReleaseEvidenceSource.GOOGLE_VERSION_HISTORY,
        module.BoronBrowserFamily.CHROME_STABLE,
        module.CHROME_VERSION,
        module.PLATFORM,
        module.CHROME_RELEASE_AT_MS,
        now_ms,
        "sha256:" + "3" * 64,
    )
    with pytest.raises(module.BuildRejected, match="registry_reinspection_mismatch"):
        module.build_images(
            now_ms=now_ms,
            release_evidence=release_evidence,
            hosted_environment=environment,
            qualification_source_digest=source_digest,
            context_archive=context_archive,
        )
    assert len(published_calls) == 2
    assert all(call["context_archive"] is context_archive for call in published_calls)
    assert all(call["qualification_source_digest"] == source_digest for call in published_calls)


def test_live_registry_reference_binds_index_to_exact_local_config(monkeypatch) -> None:
    module = _live_module()
    repository = "ghcr.io/seabass-up/algo-cli-boron-browser"
    index_digest = "sha256:" + "1" * 64
    config_digest = "sha256:" + "2" * 64
    reference = repository + "@" + index_digest
    observed = json.dumps({"config_digest": config_digest, "repo_digests": [reference]})
    commands: list[tuple[list[str], str, int]] = []

    def fake_run(args, *, stage: str, timeout: int = 60) -> str:
        commands.append((args, stage, timeout))
        return observed

    monkeypatch.setattr(module, "_run", fake_run)
    assert (
        module._registry_reference(
            repository=repository,
            index_digest=index_digest,
            config_digest=config_digest,
        )
        == reference
    )
    assert commands == [
        (
            [
                "docker",
                "image",
                "inspect",
                reference,
                "--format",
                '{"config_digest":{{json .Id}},"repo_digests":{{json .RepoDigests}}}',
            ],
            "registry_identity",
            30,
        )
    ]

    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: json.dumps({"config_digest": "sha256:" + "3" * 64, "repo_digests": [reference]}),
    )
    with pytest.raises(module.LiveSessionRejected, match="registry_identity_mismatch"):
        module._registry_reference(
            repository=repository,
            index_digest=index_digest,
            config_digest=config_digest,
        )


def test_live_browser_evidence_rejects_emulated_architecture(monkeypatch) -> None:
    module = _live_module()

    def fake_run(_args, *, stage: str, timeout: int = 60) -> str:
        assert stage == "docker_platform"
        assert timeout == 30
        return "linux/aarch64\n"

    monkeypatch.setattr(module, "_run", fake_run)
    with pytest.raises(module.LiveSessionRejected, match="live_platform_emulation_forbidden"):
        module._assert_native_amd64_docker()


@pytest.mark.parametrize("architecture", ["linux/amd64", "linux/x86_64"])
def test_live_browser_evidence_accepts_only_native_amd64(
    monkeypatch,
    architecture: str,
) -> None:
    module = _live_module()
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: architecture,
    )
    assert module._assert_native_amd64_docker() == "linux/amd64"


def test_live_browser_cross_binds_loaded_images_to_build_evidence() -> None:
    module = _live_module()
    build = {
        "browser_index_digest": "sha256:" + "1" * 64,
        "broker_index_digest": "sha256:" + "2" * 64,
        "broker_code_digest": "sha256:" + "3" * 64,
    }
    browser = module.BoronImagePin(
        "algo-cli/boron-browser@sha256:" + "1" * 64,
        module.BoronImagePurpose.PUBLIC_MANAGED,
        module.BoronBrowserFamily.CHROME_STABLE,
        module.CHROME_VERSION,
        module.PLATFORM,
        module.CHROME_RELEASE_AT_MS,
    )
    broker = module.BoronBrokerImagePin(
        "algo-cli/xenon-broker@sha256:" + "2" * 64,
        module.PLATFORM,
        "sha256:" + "3" * 64,
    )
    module._assert_build_image_binding(
        build,
        browser_image=browser,
        broker_image=broker,
    )

    for field, replacement, reason in (
        (
            "browser_index_digest",
            "sha256:" + "4" * 64,
            "live_browser_image_changed",
        ),
        (
            "broker_index_digest",
            "sha256:" + "4" * 64,
            "live_broker_image_changed",
        ),
        ("broker_code_digest", "sha256:" + "4" * 64, "live_broker_code_changed"),
    ):
        changed = dict(build)
        changed[field] = replacement
        with pytest.raises(module.LiveSessionRejected, match=reason):
            module._assert_build_image_binding(
                changed,
                browser_image=browser,
                broker_image=broker,
            )


def test_live_driver_finalizes_bounded_stderr_and_process_cleanup() -> None:
    module = _live_module()
    stderr = b"bounded-stderr"
    driver = module._FramedProcess(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stderr.buffer.write(b'bounded-stderr');"
                "sys.stderr.buffer.flush();"
                "sys.stdout.buffer.write(b'frame' + bytes([0]));"
                "sys.stdout.buffer.flush()"
            ),
        ],
        stage="test_driver",
    )
    try:
        driver.finish_input()
        assert (
            driver.read(
                deadline=module.time.monotonic() + 5,
                stage="test_driver",
            )
            == b"frame"
        )
        assert driver.wait(timeout=5, stage="test_driver") == 0
        evidence = driver.stderr_evidence
        assert evidence == {
            "byte_count": len(stderr),
            "digest": "sha256:" + hashlib.sha256(stderr).hexdigest(),
        }
        assert driver.stderr_evidence == evidence
    finally:
        assert driver.close() is True
    assert driver.process.poll() is not None


def test_live_driver_rejects_oversized_stderr(monkeypatch) -> None:
    module = _live_module()
    monkeypatch.setattr(module, "MAX_STDERR_EVIDENCE_BYTES", 32)
    driver = module._FramedProcess(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stderr.buffer.write(b'x' * 64);"
                "sys.stderr.buffer.flush();"
                "sys.stdout.buffer.write(b'frame' + bytes([0]));"
                "sys.stdout.buffer.flush()"
            ),
        ],
        stage="test_driver",
    )
    try:
        driver.finish_input()
        with pytest.raises(module.LiveSessionRejected, match="control_stderr_size"):
            driver.read(
                deadline=module.time.monotonic() + 5,
                stage="test_driver",
            )
            driver.wait(timeout=5, stage="test_driver")
    finally:
        assert driver.close() is True


def test_live_driver_close_terminates_a_hung_process() -> None:
    module = _live_module()
    driver = module._FramedProcess(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stage="test_driver",
    )
    assert driver.close() is True
    assert driver.process.poll() is not None


def test_cleanup_helpers_verify_container_and_network_absence(monkeypatch) -> None:
    module = _live_module()
    commands: list[list[str]] = []
    clock = [100.0]

    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:4] == ["docker", "container", "inspect", "browser"]:
            return module.subprocess.CompletedProcess([], 1, "", "Error: No such container: browser")
        if args[:4] == ["docker", "network", "inspect", "private"]:
            return module.subprocess.CompletedProcess([], 1, "", "network private not found")
        raise AssertionError(args)

    monkeypatch.setattr(module, "CLEANUP_ABSENCE_TIMEOUT_SECONDS", 0.12)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    session_digest = "sha256:" + "4" * 64
    assert (
        module._cleanup_container(
            "browser",
            session_digest=session_digest,
            role="managed-browser",
        )
        is True
    )
    assert (
        module._cleanup_network(
            "private",
            session_digest=session_digest,
            role="browser-internal",
        )
        is True
    )
    assert any(command[:4] == ["docker", "container", "inspect", "browser"] for command in commands)
    assert any(command[:4] == ["docker", "network", "inspect", "private"] for command in commands)
    assert all(command[2] == "inspect" for command in commands)


def test_cleanup_requires_a_verified_absence_and_preserves_primary_reason(
    monkeypatch,
) -> None:
    module = _live_module()
    resource_id = "a" * 64
    foreign = json.dumps(
        {
            "id": resource_id,
            "labels": {
                "com.algo-cli.role": "managed-browser",
                "com.algo-cli.session": "sha256:" + "9" * 64,
            },
        }
    )
    commands: list[list[str]] = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        return module.subprocess.CompletedProcess([], 0, foreign, "")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        fake_run,
    )
    assert (
        module._cleanup_container(
            "browser",
            session_digest="sha256:" + "4" * 64,
            role="managed-browser",
        )
        is False
    )
    assert len(commands) == 1
    assert commands[0][:4] == ["docker", "container", "inspect", "browser"]
    assert module._cleanup_failure_reason(None) == "cleanup_incomplete"
    assert (
        module._cleanup_failure_reason(module.LiveSessionRejected("broker_result_invariant"))
        == "broker_result_invariant_and_cleanup_incomplete"
    )
    assert module._cleanup_failure_reason(ValueError("unbounded reason!")) == "live_failure_and_cleanup_incomplete"


def test_live_evidence_limitation_matches_native_architecture_gate() -> None:
    module = _live_module()
    assert module.LIVE_EVIDENCE_LIMITATION == (
        "One live public GET on native amd64 Linux Docker; not product readiness or broad-site compatibility."
    )
    assert "emulation" not in module.LIVE_EVIDENCE_LIMITATION
