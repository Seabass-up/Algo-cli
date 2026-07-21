from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import stat

import pytest

from algo_cli import austin_release_packager as release
from algo_cli.austin_release_packager import (
    ADA_RELEASE_EVIDENCE_FILENAME,
    AustinCommandResult,
    AustinReleaseConfig,
    AustinReleasePackager,
    AustinReleaseRejected,
    AustinSubprocessRunner,
)


TEAM_ID = "ABCDE12345"
APPLICATION_IDENTITY = f"Developer ID Application: Algo CLI ({TEAM_ID})"
INSTALLER_IDENTITY = f"Developer ID Installer: Algo CLI ({TEAM_ID})"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
APP_SUBMISSION = "00000000-0000-4000-8000-000000000111"
PKG_SUBMISSION = "00000000-0000-4000-8000-000000000222"
KEY_DIGEST = "sha256:" + hashlib.sha256(bytes(range(32))).hexdigest()


def _config(tmp_path: Path) -> AustinReleaseConfig:
    key = tmp_path / "AdaAuthority.bin"
    key.write_bytes(bytes(range(32)))
    key.chmod(0o444)
    return AustinReleaseConfig(
        application_identity=APPLICATION_IDENTITY,
        installer_identity=INSTALLER_IDENTITY,
        team_id=TEAM_ID,
        notary_profile="Algo CLI Notary",
        extension_origin=ORIGIN,
        disabled_native_authority_public_key=key,
        disabled_native_authority_public_key_digest=KEY_DIGEST,
        output_directory=tmp_path / "release",
        version="0.18.0",
        build_number="1800",
    )


class FakeAustinRunner:
    def __init__(self, app: Path, *, app_issues: list[object] | None = None) -> None:
        self.app = app
        self.app_issues = app_issues or []
        self.commands: list[tuple[str, ...]] = []

    @staticmethod
    def _all_identity_output() -> bytes:
        return (
            f'  1) ABCDEF "{APPLICATION_IDENTITY}"\n  2) FEDCBA "{INSTALLER_IDENTITY}"\n     2 valid identities found\n'
        ).encode("utf-8")

    @staticmethod
    def _code_signing_identity_output() -> bytes:
        return (f'  1) ABCDEF "{APPLICATION_IDENTITY}"\n     1 valid identities found\n').encode("utf-8")

    def _stage_app(self, environment) -> None:
        contents = self.app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Helpers").mkdir()
        (contents / "Resources").mkdir()
        (contents / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": release.AUSTIN_BUNDLE_ID,
                    "CFBundleShortVersionString": environment["AUSTIN_RELEASE_VERSION"],
                    "CFBundleVersion": environment["AUSTIN_RELEASE_BUILD"],
                }
            )
        )
        for path in (
            contents / "MacOS" / "austin-control",
            contents / "Helpers" / "austin-relay",
            contents / "Helpers" / "austin-tcc-adapter",
            contents / "Helpers" / "austin-credential-migrator",
            contents / "Helpers" / "neon-native-host",
        ):
            path.write_bytes(b"signed")
            path.chmod(0o755)
        authority_source = Path(environment["AUSTIN_AUTHORITY_PUBLIC_KEY_FILE"])
        authority_target = contents / "Resources" / "AustinAuthorityPublicKey.bin"
        authority_target.write_bytes(authority_source.read_bytes())
        authority_target.chmod(0o444)

    def _details(self, path: str) -> bytes:
        if path.endswith("austin-relay"):
            identifier = "com.algo-cli.austin.relay"
        elif path.endswith("austin-tcc-adapter"):
            identifier = "com.algo-cli.austin.tcc-adapter"
        elif path.endswith("austin-credential-migrator"):
            identifier = "com.algo-cli.austin.credential-migrator"
        elif path.endswith("neon-native-host"):
            identifier = "com.algo-cli.neon.host"
        else:
            identifier = release.AUSTIN_BUNDLE_ID
        return (
            f"Identifier={identifier}\n"
            "CodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+1\n"
            f"Authority={APPLICATION_IDENTITY}\n"
            "Authority=Developer ID Certification Authority\n"
            "Authority=Apple Root CA\n"
            f"TeamIdentifier={TEAM_ID}\n"
        ).encode("utf-8")

    @staticmethod
    def _entitlements(path: str) -> bytes:
        if path.endswith("austin-tcc-adapter"):
            value = {"com.apple.security.automation.apple-events": True}
        elif path.endswith(("neon-native-host", "austin-credential-migrator")):
            value = {}
        else:
            value = {
                "com.apple.security.app-sandbox": True,
                "com.apple.security.application-groups": ["group.com.algo-cli.control"],
            }
        return plistlib.dumps(value)

    def run(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment=None,
    ) -> AustinCommandResult:
        assert 0 < timeout_seconds <= 3_600
        self.commands.append(command)
        if command == (
            "/usr/bin/security",
            "find-identity",
            "-v",
            "-p",
            "codesigning",
        ):
            return AustinCommandResult(self._code_signing_identity_output())
        if command == ("/usr/bin/security", "find-identity", "-v"):
            return AustinCommandResult(self._all_identity_output())
        if command[1:3] == ("notarytool", "history"):
            return AustinCommandResult(b'{"history":[]}')
        if command[0] == str(release.AUSTIN_BUILD_SCRIPT):
            self._stage_app(environment)
            return AustinCommandResult(b"staged")
        if command[0] == "/usr/bin/codesign" and "--entitlements" in command:
            return AustinCommandResult(self._entitlements(command[-1]))
        if command[0] == "/usr/bin/codesign" and command[1:3] == ("-d", "--verbose=4"):
            return AustinCommandResult(self._details(command[-1]))
        if command[0] == "/usr/bin/ditto":
            Path(command[-1]).write_bytes(b"signed-app-archive")
            return AustinCommandResult(b"")
        if command[1:3] == ("notarytool", "submit"):
            submission = APP_SUBMISSION if command[3].endswith(".zip") else PKG_SUBMISSION
            return AustinCommandResult(json.dumps({"id": submission, "status": "Accepted"}).encode("utf-8"))
        if command[1:3] == ("notarytool", "log"):
            issues = self.app_issues if command[3] == APP_SUBMISSION else []
            Path(command[-1]).write_text(
                json.dumps({"issues": issues, "status": "Accepted"}),
                encoding="utf-8",
            )
            Path(command[-1]).chmod(0o600)
            return AustinCommandResult(b"")
        if command[0] == "/usr/bin/pkgbuild":
            Path(command[-1]).write_bytes(b"signed-installer-package")
            return AustinCommandResult(b"")
        if command[:2] == ("/usr/sbin/pkgutil", "--check-signature"):
            return AustinCommandResult(
                (
                    f"1. {INSTALLER_IDENTITY}\n"
                    "Status: signed by a developer certificate issued by Apple for distribution\n"
                    "Signed with a trusted timestamp\n"
                ).encode("utf-8")
            )
        return AustinCommandResult(b"")


def test_two_round_release_pipeline_is_exact_and_emits_structural_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    app = tmp_path / "stage" / "Algo CLI Control.app"
    monkeypatch.setattr(release, "AUSTIN_STAGE_APP", app)
    runner = FakeAustinRunner(app)
    config = _config(tmp_path)

    result = AustinReleasePackager(runner=runner, clock_ms=lambda: 1_800_000_000_000).build(config)

    assert result.package_path.read_bytes() == b"signed-installer-package"
    assert result.evidence_path.name == ADA_RELEASE_EVIDENCE_FILENAME
    evidence = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert evidence["team_id"] == TEAM_ID
    assert evidence["app_submission_id"] == APP_SUBMISSION
    assert evidence["package_submission_id"] == PKG_SUBMISSION
    assert evidence["package_digest"] == result.package_digest
    assert evidence["native_control_protocol"] == "disabled_foundation"
    assert evidence["native_authority_public_key_digest"] == KEY_DIGEST
    assert str(tmp_path) not in result.evidence_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(result.evidence_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.package_path.stat().st_mode) == 0o644
    submits = [command for command in runner.commands if command[1:3] == ("notarytool", "submit")]
    assert len(submits) == 2
    assessments = [command for command in runner.commands if command[:2] == ("/usr/sbin/spctl", "--assess")]
    assert {command[3] for command in assessments} == {"execute", "install"}


def test_missing_identity_blocks_before_build_or_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    app = tmp_path / "stage" / "Algo CLI Control.app"
    monkeypatch.setattr(release, "AUSTIN_STAGE_APP", app)
    runner = FakeAustinRunner(app)
    runner._all_identity_output = lambda: f'1) ABCDEF "{APPLICATION_IDENTITY}"\n'.encode()
    config = _config(tmp_path)

    with pytest.raises(AustinReleaseRejected, match="austin_release_installer_identity_missing"):
        AustinReleasePackager(runner=runner).build(config)

    assert not app.exists()
    assert not config.output_directory.exists()
    assert all(command[0] == "/usr/bin/security" for command in runner.commands)


def test_application_identity_must_pass_codesigning_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    app = tmp_path / "stage" / "Algo CLI Control.app"
    monkeypatch.setattr(release, "AUSTIN_STAGE_APP", app)
    runner = FakeAustinRunner(app)
    runner._code_signing_identity_output = lambda: b"0 valid identities found\n"

    with pytest.raises(AustinReleaseRejected, match="austin_release_application_identity_missing"):
        AustinReleasePackager(runner=runner).build(_config(tmp_path))

    assert not app.exists()


def test_subprocess_runner_bounds_output_while_child_is_running() -> None:
    with pytest.raises(AustinReleaseRejected, match="austin_release_command_failed"):
        AustinSubprocessRunner().run(
            (
                release.sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))",
            ),
            timeout_seconds=10,
        )


def test_notary_warning_or_issue_is_release_blocking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    app = tmp_path / "stage" / "Algo CLI Control.app"
    monkeypatch.setattr(release, "AUSTIN_STAGE_APP", app)
    runner = FakeAustinRunner(app, app_issues=[{"severity": "warning"}])
    config = _config(tmp_path)

    with pytest.raises(AustinReleaseRejected, match="austin_release_notary_log"):
        AustinReleasePackager(runner=runner).build(config)

    assert not config.output_directory.exists()
    assert not any(command[0] == "/usr/bin/pkgbuild" for command in runner.commands)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("application_identity", "-", "austin_release_application_identity"),
        ("installer_identity", "Developer ID Installer: Other (ZZZZZ99999)", "austin_release_installer_identity"),
        ("notary_profile", "profile\nsecret", "austin_release_notary_profile"),
        ("extension_origin", "https://example.com", "austin_release_extension_origin"),
        ("version", "0.18.0-foundation", "austin_release_version"),
        ("version", "0.18.1", "austin_release_version_mismatch"),
        ("build_number", "0", "austin_release_build"),
        (
            "disabled_native_authority_public_key_digest",
            "sha256:short",
            "austin_release_authority_digest",
        ),
    ],
)
def test_release_inputs_are_closed_and_content_free(
    tmp_path: Path, monkeypatch, field: str, value: str, reason: str
) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    config = _config(tmp_path)
    values = {
        "application_identity": config.application_identity,
        "installer_identity": config.installer_identity,
        "team_id": config.team_id,
        "notary_profile": config.notary_profile,
        "extension_origin": config.extension_origin,
        "disabled_native_authority_public_key": config.disabled_native_authority_public_key,
        "disabled_native_authority_public_key_digest": (config.disabled_native_authority_public_key_digest),
        "output_directory": config.output_directory,
        "version": config.version,
        "build_number": config.build_number,
    }
    values[field] = value

    with pytest.raises(AustinReleaseRejected, match=reason):
        AustinReleaseConfig(**values).validate()


def test_authority_key_must_match_independently_retained_digest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    config = _config(tmp_path)
    values = {
        "application_identity": config.application_identity,
        "installer_identity": config.installer_identity,
        "team_id": config.team_id,
        "notary_profile": config.notary_profile,
        "extension_origin": config.extension_origin,
        "disabled_native_authority_public_key": (config.disabled_native_authority_public_key),
        "disabled_native_authority_public_key_digest": "sha256:" + "f" * 64,
        "output_directory": config.output_directory,
        "version": config.version,
        "build_number": config.build_number,
    }
    with pytest.raises(AustinReleaseRejected, match="austin_release_authority_digest"):
        AustinReleaseConfig(**values).validate()


def test_existing_output_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(release.sys, "platform", "darwin")
    config = _config(tmp_path)
    config.output_directory.mkdir()
    marker = config.output_directory / "user.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(AustinReleaseRejected, match="austin_release_output_exists"):
        config.validate()

    assert marker.read_text(encoding="utf-8") == "preserve"
