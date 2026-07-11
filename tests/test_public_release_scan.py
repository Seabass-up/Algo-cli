"""Regression tests for public-release privacy scanning."""

import base64
import hashlib
import json
import subprocess

from scripts import check_public_history, check_public_release


def test_machine_path_scan_catches_literal_and_source_escaped_windows_paths():
    literal = "C:" + "\\".join(("", "Users", "private-user", "workspace"))
    source_escaped = "C:" + "\\\\".join(("", "Users", "private-user", "workspace"))

    assert check_public_release._scan_text("fixture.py", literal)
    assert check_public_release._scan_text("fixture.py", source_escaped)


def test_machine_path_scan_allows_neutral_windows_fixture():
    assert check_public_release._scan_text("fixture.py", r"C:\\Users\\example\\workspace") == []


def test_github_service_email_is_public_but_personal_email_is_rejected():
    service_email = "noreply@" + "github.com"
    personal_email = "person@" + "gmail.com"

    assert check_public_release._scan_text("commit", service_email) == []
    assert (1, "non-public email") in check_public_release._scan_text("commit", personal_email)


def test_valid_lockfile_integrity_is_masked_but_neighboring_metadata_is_scanned():
    private_marker = "x" + "ds"
    encoded = base64.b64encode(hashlib.sha512(b"695").digest()).decode()
    assert private_marker in encoded.casefold()
    integrity = f"sha512-{encoded}"
    lockfile = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "integrity": integrity,
                    "resolved": f"https://registry.example.test/{private_marker}/package.tgz",
                }
            },
        },
        indent=2,
    )

    findings = check_public_release._scan_text("archive!project/package-lock.json", lockfile)

    assert findings
    resolved_line = next(index for index, line in enumerate(lockfile.splitlines(), 1) if '"resolved"' in line)
    assert (resolved_line, "private marker") in findings
    integrity_line = next(index for index, line in enumerate(lockfile.splitlines(), 1) if '"integrity"' in line)
    assert (integrity_line, "private marker") not in findings


def test_non_sri_lockfile_integrity_fails_closed():
    private_marker = "sco" + "tt"
    lockfile = json.dumps({"packages": {"": {"integrity": private_marker}}})

    assert check_public_release._scan_text("package-lock.json", lockfile)


def test_binary_payload_is_scanned_for_ascii_secrets():
    token = b"ghp_" + (b"c" * 24)

    findings = check_public_release._scan_item("fixture.bin", b"\x00\xff" + token)

    assert any("secret or machine path" in finding for finding in findings)


def test_oversized_payload_fails_closed(monkeypatch):
    monkeypatch.setattr(check_public_release, "TEXT_LIMIT", 8)

    findings = check_public_release._scan_item("fixture.bin", b"a" * 9)

    assert any("exceeds 8-byte scan limit" in finding for finding in findings)


def test_generated_node_modules_path_is_rejected():
    findings = check_public_release._scan_name("project/node_modules/dependency/LICENSE")

    assert "generated dependency path" in findings


def test_python_artifact_rejects_embedded_website_tree():
    findings = check_public_release._scan_artifact_name(
        "algo_cli_runtime-0.14.0.tar.gz!algo_cli_runtime-0.14.0/website/README.md"
    )

    assert findings


def test_scanner_masks_detector_definitions_but_not_unrelated_leaks():
    scanner_source = check_public_release.SELF.read_text(encoding="utf-8")
    assert check_public_release._scan_text("scripts/check_public_release.py", scanner_source) == []

    token = "ghp_" + ("a" * 24)
    findings = check_public_release._scan_text(
        "scripts/check_public_release.py",
        scanner_source + f"\nACCIDENTAL_TOKEN = {token!r}\n",
    )

    assert findings


def test_history_scan_catches_a_secret_deleted_from_head(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Example Contributor")
    git("config", "user.email", "example@users.noreply.github.com")
    token = "ghp_" + ("b" * 24)
    secret_path = tmp_path / "fixture.txt"
    secret_path.write_text(token, encoding="utf-8")
    git("add", "fixture.txt")
    git("commit", "-q", "-m", "Add fixture")
    secret_path.unlink()
    git("add", "-u")
    git("commit", "-q", "-m", "Remove fixture")

    findings = check_public_history.scan_history(tmp_path)

    assert any("fixture.txt" in finding and "secret or machine path" in finding for finding in findings)


def test_history_scan_catches_blob_referenced_only_by_tag(tmp_path):
    def git(*args: str, input_data: bytes | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            input=input_data,
            capture_output=True,
        )
        return result.stdout.decode().strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Example Contributor")
    git("config", "user.email", "example@users.noreply.github.com")
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "Initial commit")
    token = b"ghp_" + (b"d" * 24)
    object_id = git("hash-object", "-w", "--stdin", input_data=token)
    git("update-ref", "refs/tags/blob-fixture", object_id)

    findings = check_public_history.scan_history(tmp_path)

    assert any("unpathed-blob" in finding and "secret or machine path" in finding for finding in findings)


def test_history_scan_allows_external_contributor_identity_in_ci_mode(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Sco" + "tt Example")
    git("config", "user.email", "contributor@" + "users.example.org")
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "Public contribution")

    strict_findings = check_public_history.scan_history(tmp_path)

    assert any("non-public email" in finding for finding in strict_findings)
    assert check_public_history.scan_history(tmp_path, allow_contributor_identities=True) == []
