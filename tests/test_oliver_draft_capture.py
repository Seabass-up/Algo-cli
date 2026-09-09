from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import textwrap
import time
from typing import Any
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/oliver-draft-capture.yml"
SPEC = importlib.util.spec_from_file_location("draft_capture_authority", ROOT / "scripts/oliver_release_authority.py")
assert SPEC and SPEC.loader
AUTHORITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUTHORITY
SPEC.loader.exec_module(AUTHORITY)
SOURCE = "57a4740ab73a79244413a64396ee9e9f2285b738"
PUBLISHER = "a" * 40


def program() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return textwrap.dedent(workflow.split("          python -I -B -S - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])


def document(*, populated: bool = False) -> dict[str, Any]:
    rows = []
    if populated:
        for index, name in enumerate(sorted(AUTHORITY.expected_release_assets("v0.19.1")), 1):
            payload = name.encode()
            rows.append({"id": index, "name": name, "size": len(payload), "state": "uploaded",
                         "digest": "sha256:" + hashlib.sha256(payload).hexdigest()})
    return {"id": 385866827, "tag_name": "v0.19.1", "target_commitish": SOURCE,
            "draft": True, "prerelease": False, "immutable": False, "published_at": None, "assets": rows}


class Response(io.BytesIO):
    status = 200


def execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, phase: str = "initial",
    release: dict[str, Any] | None = None, listing: Any = None, drift: bool = False,
    redirect: str | None = None, corrupt: bool = False, status: int | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[urllib.request.Request]]:
    release = document() if release is None else release
    listing = [release] if listing is None else listing
    values = {
        "CAPTURE_TOKEN": "synthetic-token", "CAPTURE_PHASE": phase, "RELEASE_TAG": "v0.19.1",
        "RUNNER_TEMP": str(tmp_path), "GITHUB_REPOSITORY": "Seabass-up/Algo-cli",
        "GITHUB_ACTOR_ID": "184999458", "GITHUB_REF": "refs/heads/main", "GITHUB_REF_PROTECTED": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "99",
        "GITHUB_SHA": PUBLISHER,
    }
    values.update(environment or {})
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    calls: list[urllib.request.Request] = []
    reads = 0

    class Opener:
        def open(self, request: urllib.request.Request, timeout: int) -> Response:
            nonlocal reads
            calls.append(request)
            assert timeout == 30 and request.get_method() == "GET"
            url = request.full_url
            if url.startswith("https://release-assets.githubusercontent.com/"):
                assert request.get_header("Authorization") is None
                return Response(release["assets"][len([r for r in calls if r.full_url.startswith(url.split('?')[0])]) - 1]["name"].encode())
            assert request.get_header("Authorization") == "Bearer synthetic-token"
            prefix = "https://api.github.com/repos/Seabass-up/Algo-cli/"
            assert url.startswith(prefix)
            if status is not None:
                raise urllib.error.HTTPError(url, status, "private-canary", {}, None)
            endpoint = url.removeprefix(prefix)
            if endpoint == "releases?per_page=100":
                return Response(json.dumps(listing).encode())
            if endpoint == "releases/385866827":
                reads += 1
                value = copy.deepcopy(release)
                if drift and reads == 2:
                    value["target_commitish"] = "b" * 40
                return Response(json.dumps(value).encode())
            assert endpoint.startswith("releases/assets/")
            row = next(row for row in release["assets"] if str(row["id"]) == endpoint.split("/")[-1])
            if redirect:
                raise urllib.error.HTTPError(url, 302, "redirect", {"Location": redirect}, None)
            return Response(b"corrupt" if corrupt else row["name"].encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    exec(compile(program(), str(WORKFLOW), "exec"), {})
    receipt = json.loads((tmp_path / "draft-capture/snapshot.json").read_bytes())
    return receipt, calls


@pytest.mark.parametrize("phase,populated,published", [
    ("initial", False, False), ("initial", True, False), ("initial", True, True), ("before-pypi", True, False),
])
def test_exact_capture_preserves_identity_and_only_gets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, populated: bool, published: bool,
) -> None:
    release = document(populated=populated)
    if published:
        release.update(draft=False, immutable=True, published_at="2026-09-09T23:00:00Z")
    receipt, calls = execute(monkeypatch, tmp_path, phase=phase, release=release)
    assert receipt["release"] == release and receipt["source"] == SOURCE and receipt["publisher"] == PUBLISHER
    assert receipt["phase"] == phase and receipt["run_id"] == "99" and receipt["run_attempt"] == "1"
    expected = AUTHORITY.expected_release_assets("v0.19.1") if phase == "initial" and populated else set()
    assert {p.name for p in (tmp_path / "draft-capture/assets").iterdir()} == expected
    assert len(calls) == 3 + len(expected)


@pytest.mark.parametrize("change", [
    {"RELEASE_TAG": "v0.19.0"}, {"CAPTURE_PHASE": "other"}, {"GITHUB_REPOSITORY": "other/repo"},
    {"GITHUB_ACTOR_ID": "1"}, {"GITHUB_REF": "refs/heads/other"}, {"GITHUB_REF_PROTECTED": "false"},
    {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_SHA": "main"},
])
def test_capture_rejects_wrong_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, str]) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, environment=change)


@pytest.mark.parametrize("change", [
    {"id": 1}, {"tag_name": "v0.19.0"}, {"target_commitish": PUBLISHER}, {"prerelease": True},
    {"immutable": True}, {"published_at": "2026-09-09"}, {"assets": {}},
])
def test_capture_rejects_changed_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any]) -> None:
    release = document()
    release.update(change)
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, release=release)


@pytest.mark.parametrize("listing", [[], {}, [document(), document()], [document()] * 100])
def test_capture_rejects_missing_ambiguous_or_truncated_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, listing: Any,
) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, listing=listing)


@pytest.mark.parametrize("change", [
    {"name": "../escape"}, {"id": 0}, {"id": True}, {"size": 0}, {"size": 67108865},
    {"digest": "sha256:bad"}, {"state": "new"}, {"name": "SHA256SUMS"},
])
def test_capture_rejects_asset_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any]) -> None:
    release = document(populated=True)
    release["assets"][-1].update(change)
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, release=release)


@pytest.mark.parametrize("options", [
    {"drift": True}, {"corrupt": True}, {"status": 403}, {"status": 302},
    {"redirect": "https://evil.invalid/asset"}, {"redirect": "http://release-assets.githubusercontent.com/asset"},
    {"redirect": "https://user" + "@" + "release-assets.githubusercontent.com/asset"},
])
def test_capture_closes_transport_and_drift_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, release=document(populated=True), **options)
    captured = capsys.readouterr()
    assert "synthetic-token" not in captured.out + captured.err and "private-canary" not in captured.out + captured.err
    assert not (tmp_path / "draft-capture/snapshot.json").exists()


def test_draft_snapshot_routes_only_bound_release_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipt, _ = execute(monkeypatch, tmp_path)
    requests: list[str] = []
    api = AUTHORITY.draft_snapshot_api(receipt, environment=os.environ,
                                       api_get=lambda endpoint: requests.append(endpoint) or "live")
    assert api("repos/Seabass-up/Algo-cli/releases?per_page=100") == receipt["listing"]
    assert api("repos/Seabass-up/Algo-cli/releases/385866827") == receipt["release"]
    assert api("repos/Seabass-up/Algo-cli/branches/main") == "live"
    assert requests == ["repos/Seabass-up/Algo-cli/branches/main"]
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_draft_snapshot_endpoint"):
        api("repos/Seabass-up/Algo-cli/releases/123")


def test_signed_asset_download_does_not_receive_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipt, calls = execute(monkeypatch, tmp_path, release=document(populated=True),
                             redirect="https://release-assets.githubusercontent.com/asset?signature=synthetic")
    downloads = [r for r in calls if r.full_url.startswith("https://release-assets.githubusercontent.com/")]
    assert len(downloads) == len(AUTHORITY.expected_release_assets("v0.19.1"))
    assert all(r.get_header("Authorization") is None for r in downloads)
    assert receipt["release"]["draft"] is True


@pytest.mark.parametrize("phase,published", [("before-pypi", False), ("initial", True)])
def test_incomplete_assets_cannot_authorize_upload_or_published_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, published: bool,
) -> None:
    release = document()
    if published:
        release.update(draft=False, immutable=True, published_at="2026-09-09T23:00:00Z")
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, phase=phase, release=release)


@pytest.mark.parametrize("key,value", [
    ("publisher", "b" * 40), ("source", "b" * 40), ("run_id", "100"), ("run_attempt", "2"),
    ("tag", "v0.19.0"), ("release_id", 1), ("phase", "before-pypi"), ("schema_version", 2),
    ("captured_at", 0), ("captured_at", 9999999999), ("captured_at", True),
])
def test_draft_snapshot_rejects_stale_or_misbound_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, value: Any,
) -> None:
    receipt, _ = execute(monkeypatch, tmp_path)
    receipt[key] = value
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_draft_snapshot"):
        AUTHORITY.draft_snapshot_api(receipt, environment=os.environ, api_get=lambda _: pytest.fail("unexpected API"))


@pytest.mark.parametrize("age,passed", [(0, True), (119, True), (121, False), (-10, False)])
def test_final_upload_freshness_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, age: int, passed: bool) -> None:
    workflow = (ROOT / ".github/workflows/oliver-release.yml").read_text(encoding="utf-8")
    section = workflow.split("      - name: Reject draft evidence aged during approval or final validation\n", 1)[1]
    code = textwrap.dedent(section.split("          python -I -B -S - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    folder = tmp_path / "draft-publish-capture"
    folder.mkdir()
    (folder / "snapshot.json").write_text(json.dumps({"captured_at": int(time.time()) - age}), encoding="utf-8")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    if passed:
        exec(compile(code, "final-freshness", "exec"), {})
    else:
        with pytest.raises(SystemExit, match="release_draft_snapshot_expired"):
            exec(compile(code, "final-freshness", "exec"), {})


def test_capture_is_gated_no_checkout_no_execution_of_downloaded_bytes() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "environment: release-authority" in workflow and "contents: write" in workflow
    for forbidden in ("actions/checkout", "actions/download-artifact", "id-token: write", "secrets.",
                      "pip install", "subprocess", "exec(", "eval(", "method='POST'", "method='PATCH'", "method='DELETE'"):
        assert forbidden not in workflow
    assert "class NoRedirect" in workflow
    assert "release-assets.githubusercontent.com" in workflow
    assert "phase == 'initial'" in workflow
