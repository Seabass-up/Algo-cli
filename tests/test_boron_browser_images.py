from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


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


def _version_payload(
    *,
    extra: dict[str, object] | None = None,
    next_page_token: str = "879489531",
) -> bytes:
    document: dict[str, object] = {
        "versions": [
            {
                "name": (
                    "chrome/platforms/linux/channels/stable/versions/"
                    "150.0.7871.128"
                ),
                "version": "150.0.7871.128",
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
    start_time: object = "2026-07-16T20:53:47.785001Z",
) -> bytes:
    return json.dumps(
        {
            "releases": [
                {
                    "name": (
                        "chrome/platforms/linux/channels/stable/versions/"
                        "150.0.7871.128/releases/1784235227"
                    ),
                    "serving": {"startTime": start_time},
                    "fraction": fraction,
                    "version": "150.0.7871.128",
                    "fractionGroup": "1",
                    "pinnable": True,
                    "rolloutData": [],
                }
            ],
            "nextPageToken": "",
        },
        separators=(",", ":"),
    ).encode("ascii")


def test_dockerfiles_pin_base_downloads_users_and_narrow_copy_surface() -> None:
    browser = (RESOURCE / "boron_public_browser.Dockerfile").read_text(encoding="utf-8")
    broker = (RESOURCE / "xenon_egress_broker.Dockerfile").read_text(encoding="utf-8")
    native = (RESOURCE / "carbon_native_browser.Dockerfile").read_text(encoding="utf-8")
    for source, role, user in (
        (browser, "managed-browser", "1000:1000"),
        (broker, "egress-broker", "1001:1001"),
    ):
        assert "FROM --platform=linux/amd64 debian:bookworm-slim@sha256:" in source
        assert f'com.algo-cli.role="{role}"' in source
        assert f"USER {user}" in source
        assert "sha256sum --check --strict" in source
        assert "--proto '=https' --tlsv1.2" in source
        assert "COPY algo_cli/ " not in source
        assert "COPY . " not in source
        assert ":latest" not in source
        assert "ADD " not in source
        assert "pip install" not in source
    assert "google-chrome-stable_150.0.7871.128-1_amd64.deb" in browser
    assert 'com.algo-cli.browser.release-at-ms="1784235227785"' in browser
    assert "dpkg-query -W -f='${Version}' google-chrome-stable" in browser
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


def test_launchers_are_fixed_isolated_python_modules() -> None:
    assert (RESOURCE / "boron_browser_wrapper.sh").read_text(encoding="utf-8") == (
        "#!/bin/sh\nset -eu\n\nexec /usr/bin/python3 -B -I -u -m "
        "algo_cli.boron_browser_entry\n"
    )
    assert (RESOURCE / "xenon_egress_broker.sh").read_text(encoding="utf-8") == (
        "#!/bin/sh\nset -eu\n\nexec /usr/bin/python3 -B -I -u -m "
        "algo_cli.xenon_browser_entry\n"
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


def test_version_history_parser_is_strict_and_exact() -> None:
    module = _build_module()
    version = module._parse_latest_version(_version_payload())
    assert version == module.CHROME_VERSION
    assert (
        module._parse_latest_release(_release_payload(), version=version)
        == module.CHROME_RELEASE_AT_MS
    )
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
    evidence = module.fetch_browser_release_evidence(
        observed_at_ms=module.CHROME_RELEASE_AT_MS + 1_000
    )
    assert requested == [
        module.VERSION_HISTORY_URL,
        (
            module.VERSION_RELEASE_URL_PREFIX
            + module.CHROME_VERSION
            + "/releases?pageSize=1"
        ),
    ]
    assert evidence.browser_version == module.CHROME_VERSION
    assert evidence.security_release_at_ms == module.CHROME_RELEASE_AT_MS
    assert evidence.source_digest.startswith("sha256:")
    assert evidence.source_digest == module._release_source_digest(
        version=module.CHROME_VERSION,
        release_at_ms=module.CHROME_RELEASE_AT_MS,
    )
    assert module._parse_latest_version(
        _version_payload(next_page_token="different-opaque-token")
    ) == module.CHROME_VERSION


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
        "150.0.7871.129",
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
        "browser_image_id": "sha256:" + "1" * 64,
        "broker_image_id": "sha256:" + "2" * 64,
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
        ("browser_image_id", "sha256:" + "4" * 64, "live_browser_image_changed"),
        ("broker_image_id", "sha256:" + "4" * 64, "live_broker_image_changed"),
        ("broker_code_digest", "sha256:" + "4" * 64, "live_broker_binary_changed"),
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
        assert driver.read(
            deadline=module.time.monotonic() + 5,
            stage="test_driver",
        ) == b"frame"
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


def test_live_evidence_limitation_matches_native_architecture_gate() -> None:
    module = _live_module()
    assert module.LIVE_EVIDENCE_LIMITATION == (
        "One live public GET on native amd64 Linux Docker; not product readiness "
        "or broad-site compatibility."
    )
    assert "emulation" not in module.LIVE_EVIDENCE_LIMITATION
