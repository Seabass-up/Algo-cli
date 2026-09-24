from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
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
SOURCE_VERSION_FILE = (ROOT / "algo_cli/__init__.py").read_text(encoding="utf-8")
TAG = "v" + re.findall(r'^__version__ = "([^"\n]*)"$', SOURCE_VERSION_FILE, re.MULTILINE)[0]
RELEASE_ID = 432100001
PUBLISHER = "a" * 40
SOURCE = PUBLISHER


def program() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return textwrap.dedent(workflow.split("          python -I -B -S - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])


def document(*, populated: bool = False, release_id: int = RELEASE_ID) -> dict[str, Any]:
    rows = []
    if populated:
        for index, name in enumerate(sorted(AUTHORITY.expected_release_assets(TAG)), 1):
            payload = name.encode()
            rows.append({"id": index, "name": name, "size": len(payload), "state": "uploaded",
                         "digest": "sha256:" + hashlib.sha256(payload).hexdigest()})
    return {"id": release_id, "tag_name": TAG, "target_commitish": SOURCE,
            "draft": True, "prerelease": False, "immutable": False, "published_at": None, "assets": rows}


def contents(text: str = SOURCE_VERSION_FILE) -> dict[str, Any]:
    encoded = base64.b64encode(text.encode()).decode()
    return {"type": "file", "path": "algo_cli/__init__.py", "encoding": "base64",
            "content": "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))}


class Response(io.BytesIO):
    status = 200


def execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, phase: str = "initial",
    release: dict[str, Any] | None = None, listing: Any = None, drift: bool = False,
    redirect: str | None = None, corrupt: bool = False, status: int | None = None,
    environment: dict[str, str] | None = None, version_file: Any = None,
) -> tuple[dict[str, Any], list[urllib.request.Request]]:
    release = document() if release is None else release
    listing = [release] if listing is None else listing
    matches = [row for row in listing if type(row) is dict and row.get("tag_name") == TAG] if type(listing) is list else []
    detail_id = matches[0].get("id") if matches else release["id"]
    values = {
        "CAPTURE_TOKEN": "synthetic-token", "CAPTURE_PHASE": phase, "RELEASE_TAG": TAG,
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
            if endpoint == f"contents/algo_cli/__init__.py?ref={values['GITHUB_SHA']}":
                return Response(json.dumps(contents() if version_file is None else version_file).encode())
            if endpoint == "releases?per_page=100":
                return Response(json.dumps(listing).encode())
            if endpoint == f"releases/{detail_id}":
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
    expected = AUTHORITY.expected_release_assets(TAG) if phase == "initial" and populated else set()
    assert {p.name for p in (tmp_path / "draft-capture/assets").iterdir()} == expected
    assert len(calls) == 4 + len(expected)
    assert calls[0].full_url == f"https://api.github.com/repos/Seabass-up/Algo-cli/contents/algo_cli/__init__.py?ref={PUBLISHER}"


@pytest.mark.parametrize("change", [
    {"RELEASE_TAG": "v0.19.0"}, {"CAPTURE_PHASE": "other"}, {"GITHUB_REPOSITORY": "other/repo"},
    {"GITHUB_ACTOR_ID": "1"}, {"GITHUB_REF": "refs/heads/other"}, {"GITHUB_REF_PROTECTED": "false"},
    {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_SHA": "main"},
])
def test_capture_rejects_wrong_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, str]) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, environment=change)


@pytest.mark.parametrize("change", [
    {"id": True}, {"tag_name": "v0.19.0"}, {"target_commitish": "main"}, {"prerelease": True},
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


def test_capture_derives_positive_release_id_from_exact_tag_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    release = document(release_id=987654321)
    receipt, calls = execute(monkeypatch, tmp_path, release=release)
    assert receipt["release_id"] == 987654321
    assert [request.full_url for request in calls].count(
        "https://api.github.com/repos/Seabass-up/Algo-cli/releases/987654321"
    ) == 2


def test_capture_rejects_boolean_detail_id_for_numeric_listing_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    release = document(release_id=1)
    release["id"] = True
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, release=release, listing=[document(release_id=1)])


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
    assert api(f"repos/Seabass-up/Algo-cli/releases/{RELEASE_ID}") == receipt["release"]
    assert api("repos/Seabass-up/Algo-cli/branches/main") == "live"
    assert requests == ["repos/Seabass-up/Algo-cli/branches/main"]
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_draft_snapshot_endpoint"):
        api("repos/Seabass-up/Algo-cli/releases/123")


def test_capture_preserves_tagged_source_when_publisher_main_advances(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    tagged_source = "b" * 40
    release = document(populated=True)
    release["target_commitish"] = tagged_source
    receipt, _ = execute(monkeypatch, tmp_path, release=release)
    assert receipt["publisher"] == PUBLISHER
    assert receipt["source"] == tagged_source
    api = AUTHORITY.draft_snapshot_api(
        receipt, environment=os.environ, api_get=lambda endpoint: endpoint,
    )
    assert api(f"repos/Seabass-up/Algo-cli/releases/{RELEASE_ID}")["target_commitish"] == tagged_source


def test_signed_asset_download_does_not_receive_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipt, calls = execute(monkeypatch, tmp_path, release=document(populated=True),
                             redirect="https://release-assets.githubusercontent.com/asset?signature=synthetic")
    downloads = [r for r in calls if r.full_url.startswith("https://release-assets.githubusercontent.com/")]
    assert len(downloads) == len(AUTHORITY.expected_release_assets(TAG))
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


@pytest.mark.parametrize("source", [None, "main", "A" * 40])
def test_draft_snapshot_requires_exact_environment_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str | None,
) -> None:
    receipt, _ = execute(monkeypatch, tmp_path)
    environment = dict(os.environ)
    if source is None:
        environment.pop("GITHUB_SHA", None)
    else:
        environment["GITHUB_SHA"] = source
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_draft_snapshot"):
        AUTHORITY.draft_snapshot_api(receipt, environment=environment, api_get=lambda _: pytest.fail("unexpected API"))


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


def test_capture_tag_and_asset_names_follow_source_version() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"\d+\.\d+\.\d+", program().split("tag_grammar", 1)[1].split("version_endpoint", 1)[0]) is None
    assert re.search(r"algo_cli_runtime-\d", workflow) is None and re.search(r"'v\d+\.\d+", workflow) is None
    assert AUTHORITY.source_release_tag() == TAG


@pytest.mark.parametrize("version_file", [
    contents(SOURCE_VERSION_FILE.replace(TAG[1:], "9.9.9")),
    contents(SOURCE_VERSION_FILE + '__version__ = "' + TAG[1:] + '"\n'),
    contents(SOURCE_VERSION_FILE.replace('__version__ = "', "__version__ = '").replace(TAG[1:] + '"', TAG[1:] + "'")),
    contents("no version here\n"),
    {**contents(), "type": "dir"}, {**contents(), "path": "other/__init__.py"}, {**contents(), "encoding": "none"},
    {**contents(), "content": "@@not-base64@@"}, {**contents(), "content": None}, [contents()],
])
def test_capture_rejects_tag_not_equal_to_dispatched_source_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version_file: Any,
) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, version_file=version_file)
    assert not (tmp_path / "draft-capture/snapshot.json").exists()


@pytest.mark.parametrize("tag", ["v9.9.9", "0.20.1", "v0.20.1.dev1", "v0.20.1+local", "v00.1.0", ""])
def test_capture_rejects_tags_other_than_the_source_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str,
) -> None:
    with pytest.raises(SystemExit, match="release_draft_capture"):
        execute(monkeypatch, tmp_path, environment={"RELEASE_TAG": tag})


def test_capture_reads_version_only_at_the_dispatched_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    other = "c" * 40
    receipt, calls = execute(monkeypatch, tmp_path, environment={"GITHUB_SHA": other})
    assert receipt["publisher"] == other
    assert calls[0].full_url.endswith(f"contents/algo_cli/__init__.py?ref={other}")


def _source_root(tmp_path: Path, text: str) -> Path:
    package = tmp_path / "source" / "algo_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(text, encoding="utf-8")
    return tmp_path / "source"


def test_draft_snapshot_binds_tag_to_checkout_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipt, _ = execute(monkeypatch, tmp_path)
    monkeypatch.setattr(AUTHORITY, "ROOT", _source_root(tmp_path, SOURCE_VERSION_FILE.replace(TAG[1:], "9.9.9")))
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_draft_snapshot"):
        AUTHORITY.draft_snapshot_api(receipt, environment=os.environ, api_get=lambda _: pytest.fail("unexpected API"))


@pytest.mark.parametrize("text", [
    "", "no version\n", '__version__ = "0.20.1"\n__version__ = "0.20.1"\n', '__version__ = "0.20.1.dev1"\n',
    "__version__ = '0.20.1'\n", '__version__ = "v0.20.1"\n', '__version__ = "0.20.1+local"\n',
])
def test_source_release_tag_rejects_ambiguous_or_noncanonical_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str,
) -> None:
    monkeypatch.setattr(AUTHORITY, "ROOT", _source_root(tmp_path, text))
    with pytest.raises(AUTHORITY.ReleaseAuthorityRejected, match="release_source_version"):
        AUTHORITY.source_release_tag()


def test_source_release_tag_reads_a_crlf_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _source_root(tmp_path, "")
    (root / "algo_cli" / "__init__.py").write_bytes(b'"""Algo CLI."""\r\n\r\n__version__ = "0.21.0"\r\n')
    monkeypatch.setattr(AUTHORITY, "ROOT", root)
    assert AUTHORITY.source_release_tag() == "v0.21.0"


@pytest.mark.parametrize("version", ["0.21.0", "1.0.0.post2"])
def test_source_release_tag_follows_a_version_bump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str) -> None:
    monkeypatch.setattr(AUTHORITY, "ROOT", _source_root(tmp_path, f'__version__ = "{version}"\n'))
    assert AUTHORITY.source_release_tag() == "v" + version
