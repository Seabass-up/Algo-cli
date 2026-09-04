from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import time
from types import ModuleType
from typing import Any
import zipfile

import pytest


if os.name == "nt":
    pytest.skip(
        "Oliver immutable source capture is intentionally confined to the ubuntu-24.04 release workflow",
        allow_module_level=True,
    )


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oliver_release_source_binding.py"
SOURCE_DATE_EPOCH = 1_700_000_000


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("oliver_release_source_binding_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BINDING = _load_module()


def test_release_source_binding_platform_boundary_is_fail_closed() -> None:
    BINDING._require_release_platform("posix")
    with pytest.raises(BINDING.SourceBindingRejected, match="platform_unsupported"):
        BINDING._require_release_platform("nt")


def _git(repository: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _write(repository: Path, relative: str, payload: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _project_toml(*, build_backend: str = "hatchling.build") -> str:
    return f"""\
[build-system]
requires = ["hatchling==1.31.0"]
build-backend = "{build_backend}"

[project]
name = "algo-cli-runtime"
dynamic = ["version"]
readme = "README.md"
license = {{ file = "LICENSE" }}

[project.scripts]
algo-cli = "algo_cli.main:main"

[tool.hatch.version]
path = "algo_cli/__init__.py"

[tool.hatch.build.targets.wheel]
packages = ["algo_cli", "ollama_cli"]

[tool.hatch.build.targets.wheel.force-include]
"docs/ALGO.md" = "algo_cli/resources/docs/ALGO.md"
"skills" = "algo_cli/resources/skills"

[tool.hatch.build.targets.sdist]
include = [
    "/README.md",
    "/LICENSE",
    "/pyproject.toml",
    "/uv.lock",
    "/algo_cli",
    "/ollama_cli",
    "/docs/ALGO.md",
    "/skills/*.md",
]
"""


def _lock_toml(*, hatchling_version: str = "1.31.0") -> str:
    return f"""\
version = 1

[[package]]
name = "build"
version = "1.5.0"

[[package]]
name = "hatchling"
version = "{hatchling_version}"
"""


def _repository(
    tmp_path: Path,
    *,
    export_ignore: bool = False,
    source_symlink: bool = False,
    hatchling_version: str = "1.31.0",
    build_backend: str = "hatchling.build",
) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release@example.test")
    _git(repository, "config", "core.autocrlf", "false")
    _write(repository, "pyproject.toml", _project_toml(build_backend=build_backend))
    _write(repository, "README.md", "# fixture\n")
    _write(repository, "LICENSE", "fixture license\n")
    _write(repository, "uv.lock", _lock_toml(hatchling_version=hatchling_version))
    _write(repository, "algo_cli/__init__.py", '__version__ = "1.2.3"\n')
    _write(repository, "algo_cli/main.py", "def main():\n    return 0\n")
    _write(repository, "algo_cli/empty.txt", "")
    _write(repository, "ollama_cli/__init__.py", "from algo_cli.main import main\n")
    _write(repository, "docs/ALGO.md", "algorithm fixture\n")
    _write(repository, "skills/example.md", "skill fixture\n")
    if export_ignore:
        _write(repository, ".gitattributes", "skills/example.md export-ignore\n")
    if source_symlink:
        (repository / "source-link").symlink_to("README.md")
    os.chmod(repository / "algo_cli/main.py", 0o755)
    _git(repository, "add", "--all")
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": f"@{SOURCE_DATE_EPOCH} +0000",
            "GIT_COMMITTER_DATE": f"@{SOURCE_DATE_EPOCH} +0000",
        }
    )
    _git(repository, "commit", "--quiet", "-m", "fixture", environment=environment)
    return repository, _git(repository, "rev-parse", "HEAD")


def _paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "archive": tmp_path / "source.tar",
        "receipt": tmp_path / "receipt.json",
        "stage": tmp_path / "stage-a",
        "rebuild_stage": tmp_path / "stage-b",
        "dist": tmp_path / "dist-a",
        "rebuild_dist": tmp_path / "dist-b",
        "bound_dist": tmp_path / "dist-bound",
        "manifest": tmp_path / "manifest.json",
    }


def _capture(repository: Path, revision: str, paths: dict[str, Path]) -> dict[str, Any]:
    receipt = BINDING.capture_revision(
        repository=repository,
        revision=revision,
        archive_path=paths["archive"],
        stage_path=paths["stage"],
        receipt_path=paths["receipt"],
    )
    BINDING.materialize_stage(
        archive_path=paths["archive"],
        receipt_path=paths["receipt"],
        stage_path=paths["rebuild_stage"],
        expected_revision=revision,
    )
    return receipt


def _stage_entries(stage: Path) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            relative = path.relative_to(stage).as_posix()
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            entries[relative] = BINDING._ArchiveEntry(relative, mode, path.read_bytes())
    return entries


def _tar_blob(files: dict[str, bytes], *, version: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative, payload in sorted(files.items()):
            info = tarfile.TarInfo(f"algo_cli_runtime-{version}/{relative}")
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = SOURCE_DATE_EPOCH
            archive.addfile(info, io.BytesIO(payload))
    return gzip.compress(stream.getvalue(), compresslevel=9, mtime=SOURCE_DATE_EPOCH)


def _zip_blob(files: dict[str, bytes], *, stored: bool = False, fifo_path: str | None = None) -> bytes:
    stream = io.BytesIO()
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    date_time = time.gmtime(SOURCE_DATE_EPOCH)[:6]
    with zipfile.ZipFile(stream, mode="w", compression=compression, compresslevel=None if stored else 9) as archive:
        for relative, payload in sorted(files.items()):
            info = zipfile.ZipInfo(relative, date_time=date_time)
            info.compress_type = compression
            info.create_system = 3
            file_type = stat.S_IFIFO if relative == fifo_path else stat.S_IFREG
            info.external_attr = (file_type | 0o644) << 16
            archive.writestr(info, payload)
    return stream.getvalue()


def _artifacts(
    stage: Path,
    dist: Path,
    *,
    stored_wheel: bool = False,
    source_override: tuple[str, bytes] | None = None,
    corrupt_record: bool = False,
    injected_metadata: bytes = b"",
    fifo_wheel_member: str | None = None,
) -> None:
    entries = _stage_entries(stage)
    version, project, _version_path = BINDING._package_configuration(entries)
    patterns = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    sdist_paths = BINDING._included_source_paths(patterns, entries)
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: algo-cli-runtime\n"
        f"Version: {version}\n"
        "License-File: LICENSE\n"
        "Description-Content-Type: text/markdown\n"
    ).encode()
    metadata += injected_metadata
    metadata += b"\n" + entries["README.md"].payload
    sdist = {path: entries[path].payload for path in sdist_paths}
    sdist["PKG-INFO"] = metadata

    wheel_mapping = BINDING._wheel_source_mapping(project, entries)
    wheel = {destination: sdist[source] for destination, source in wheel_mapping.items()}
    if source_override is not None:
        wheel[source_override[0]] = source_override[1]
    normalized = version.replace("-", "_")
    dist_info = f"algo_cli_runtime-{normalized}.dist-info"
    wheel[f"{dist_info}/METADATA"] = metadata
    wheel[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: hatchling 1.31.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
    )
    wheel[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\nalgo-cli = algo_cli.main:main\n"
    wheel[f"{dist_info}/licenses/LICENSE"] = sdist["LICENSE"]
    record_path = f"{dist_info}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for relative, payload in sorted(wheel.items()):
        writer.writerow([relative, BINDING._record_digest(payload), str(len(payload))])
    writer.writerow([record_path, "", ""])
    wheel[record_path] = record.getvalue().encode()
    if corrupt_record:
        wheel[record_path] += b"unexpected,sha256=bad,1\n"

    dist.mkdir()
    (dist / f"algo_cli_runtime-{version}.tar.gz").write_bytes(_tar_blob(sdist, version=version))
    (dist / f"algo_cli_runtime-{normalized}-py3-none-any.whl").write_bytes(
        _zip_blob(wheel, stored=stored_wheel, fifo_path=fifo_wheel_member)
    )


def _bind(paths: dict[str, Path]) -> dict[str, Any]:
    expected_revision = json.loads(paths["receipt"].read_text(encoding="utf-8"))["source"]["revision"]
    return BINDING.bind_release(
        archive_path=paths["archive"],
        expected_revision=expected_revision,
        stage_path=paths["stage"],
        rebuild_stage_path=paths["rebuild_stage"],
        receipt_path=paths["receipt"],
        dist_path=paths["dist"],
        rebuild_dist_path=paths["rebuild_dist"],
        bound_dist_path=paths["bound_dist"],
        tool_lock_path=paths["stage"] / "uv.lock",
        manifest_path=paths["manifest"],
    )


def _prepare_release(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    receipt = _capture(repository, revision, paths)
    _artifacts(paths["stage"], paths["dist"])
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"])
    return paths, receipt


def test_capture_binds_commit_not_dirty_worktree_and_materializes_direct_root(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    original = (repository / "algo_cli/main.py").read_bytes()
    os.chmod(repository / "algo_cli/main.py", 0o644)
    (repository / "algo_cli/main.py").write_text("malicious transient bytes\n", encoding="utf-8")

    receipt = _capture(repository, revision, paths)

    assert receipt["source"]["revision"] == revision
    assert (paths["stage"] / "algo_cli/main.py").read_bytes() == original
    assert not (paths["stage"] / "source").exists()
    assert stat.S_IMODE(paths["stage"].stat().st_mode) == 0o555
    assert (
        BINDING.verify_stage(
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
            expected_revision=revision,
        )
        == receipt
    )


def test_capture_ignores_git_replace_objects(tmp_path: Path) -> None:
    repository, original_revision = _repository(tmp_path)
    original = (repository / "README.md").read_bytes()
    (repository / "README.md").write_text("replacement-object payload\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": f"@{SOURCE_DATE_EPOCH + 1} +0000",
            "GIT_COMMITTER_DATE": f"@{SOURCE_DATE_EPOCH + 1} +0000",
        }
    )
    _git(repository, "commit", "--quiet", "-m", "replacement", environment=environment)
    replacement_revision = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", original_revision, replacement_revision)
    paths = _paths(tmp_path)

    _capture(repository, original_revision, paths)

    assert (paths["stage"] / "README.md").read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="Windows does not unlink an open mkstemp file")
def test_capture_streams_to_held_descriptor_not_attacker_replaced_temp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"must survive")
    original_mkstemp = BINDING.tempfile.mkstemp
    original_git_to_descriptor = BINDING._git_to_descriptor
    reserved: list[Path] = []
    observed_arguments: list[str] = []

    def reserve(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = original_mkstemp(*args, **kwargs)
        reserved.append(Path(name))
        return descriptor, name

    def attack(repo: Path, arguments: list[str], descriptor: int) -> None:
        assert reserved and not reserved[0].exists()
        reserved[0].symlink_to(victim)
        observed_arguments.extend(arguments)
        original_git_to_descriptor(repo, arguments, descriptor)

    monkeypatch.setattr(BINDING.tempfile, "mkstemp", reserve)
    monkeypatch.setattr(BINDING, "_git_to_descriptor", attack)

    BINDING.capture_revision(
        repository=repository,
        revision=revision,
        archive_path=paths["archive"],
        stage_path=paths["stage"],
        receipt_path=paths["receipt"],
    )

    assert victim.read_bytes() == b"must survive"
    assert all(not argument.startswith("--output=") for argument in observed_arguments)
    assert reserved[0].is_symlink()
    reserved[0].unlink()


def test_oversized_git_blob_is_rejected_before_archive_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _revision = _repository(tmp_path)
    oversized = repository / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.truncate(BINDING.MAX_FILE_BYTES + 1)
    _git(repository, "add", "oversized.bin")
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_DATE": f"@{SOURCE_DATE_EPOCH + 2} +0000",
            "GIT_COMMITTER_DATE": f"@{SOURCE_DATE_EPOCH + 2} +0000",
        }
    )
    _git(repository, "commit", "--quiet", "-m", "oversized", environment=environment)
    revision = _git(repository, "rev-parse", "HEAD")
    archive_called = False

    def reject_archive(_repository: Path, _arguments: list[str], _descriptor: int) -> None:
        nonlocal archive_called
        archive_called = True

    monkeypatch.setattr(BINDING, "_git_to_descriptor", reject_archive)
    paths = _paths(tmp_path)

    with pytest.raises(BINDING.SourceBindingRejected, match="source_tree_size"):
        BINDING.capture_revision(
            repository=repository,
            revision=revision,
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
        )

    assert archive_called is False


def test_git_binary_rejects_writable_path_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_bytes(b"not trusted")
    os.chmod(fake_git, 0o755)
    original_exists = BINDING.Path.exists

    def fixed_missing(path: Path) -> bool:
        if path == Path("/usr/bin/git"):
            return False
        return original_exists(path)

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(BINDING.Path, "exists", fixed_missing)
    monkeypatch.setattr(BINDING.shutil, "which", lambda _name: str(fake_git))

    with pytest.raises(BINDING.SourceBindingRejected, match="git_untrusted"):
        BINDING._git_binary()


@pytest.mark.parametrize("tree_hazard", ["export-ignore", "symlink"])
def test_capture_rejects_git_tree_archive_divergence_and_non_regular_sources(tmp_path: Path, tree_hazard: str) -> None:
    repository, revision = _repository(
        tmp_path,
        export_ignore=tree_hazard == "export-ignore",
        source_symlink=tree_hazard == "symlink",
    )
    paths = _paths(tmp_path)

    with pytest.raises(BINDING.SourceBindingRejected):
        BINDING.capture_revision(
            repository=repository,
            revision=revision,
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
        )

    assert not paths["archive"].exists()
    assert not paths["stage"].exists()
    assert not paths["receipt"].exists()


@pytest.mark.parametrize("mutation", ["replace", "symlink", "hardlink"])
def test_verify_stage_rejects_replace_symlink_and_hardlink_mutations(tmp_path: Path, mutation: str) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    parent = paths["stage"] / "algo_cli"
    target = parent / "main.py"
    payload = target.read_bytes()
    os.chmod(paths["stage"], 0o755)
    os.chmod(parent, 0o755)
    target.unlink()
    if mutation == "replace":
        target.write_bytes(b"replacement\n")
        os.chmod(target, 0o555)
    elif mutation == "symlink":
        target.symlink_to(paths["stage"] / "README.md")
    else:
        external = tmp_path / "external"
        external.write_bytes(payload)
        os.link(external, target)
        os.chmod(target, 0o555)

    with pytest.raises(BINDING.SourceBindingRejected):
        BINDING.verify_stage(
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
            expected_revision=revision,
        )


@pytest.mark.parametrize("mutation", ["file-mode", "directory-mode", "empty-directory"])
def test_verify_stage_rejects_permission_and_directory_shape_mutations(tmp_path: Path, mutation: str) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    if mutation == "file-mode":
        os.chmod(paths["stage"] / "README.md", 0o400)
    elif mutation == "directory-mode":
        os.chmod(paths["stage"] / "algo_cli", 0o500)
    else:
        os.chmod(paths["stage"], 0o755)
        (paths["stage"] / "unexpected-empty").mkdir(mode=0o555)
        os.chmod(paths["stage"], 0o555)

    with pytest.raises(BINDING.SourceBindingRejected):
        BINDING.verify_stage(
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
            expected_revision=revision,
        )


def test_cleanup_unlinks_stage_root_symlink_without_traversing_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    protected = victim / "keep.txt"
    protected.write_text("must survive\n", encoding="utf-8")
    stage = tmp_path / "stage-link"
    stage.symlink_to(victim, target_is_directory=True)

    BINDING._remove_stage(stage)

    assert not stage.exists()
    assert not stage.is_symlink()
    assert protected.read_text(encoding="utf-8") == "must survive\n"


def test_exclusive_output_rejects_parent_swap_without_writing_attacker_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "output-parent"
    parent.mkdir()
    held_parent = tmp_path / "held-parent"
    attacker_parent = tmp_path / "attacker-parent"
    output = parent / "receipt.json"
    original = BINDING._open_parent_descriptor

    def swap(path: Path, *, stage: str) -> tuple[int, os.stat_result]:
        descriptor, identity = original(path, stage=stage)
        parent.rename(held_parent)
        attacker_parent.mkdir()
        parent.symlink_to(attacker_parent, target_is_directory=True)
        return descriptor, identity

    monkeypatch.setattr(BINDING, "_open_parent_descriptor", swap)

    with pytest.raises(BINDING.SourceBindingRejected, match="output_write"):
        BINDING._write_exclusive(output, b"{}\n")

    assert not (held_parent / output.name).exists()
    assert not (attacker_parent / output.name).exists()


def test_materialize_rejects_parent_swap_without_writing_attacker_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    stage_parent = tmp_path / "materialize-parent"
    stage_parent.mkdir()
    held_parent = tmp_path / "held-materialize-parent"
    attacker_parent = tmp_path / "attacker-materialize-parent"
    stage = stage_parent / "stage"
    original = BINDING._open_parent_descriptor

    def swap(path: Path, *, stage: str) -> tuple[int, os.stat_result]:
        descriptor, identity = original(path, stage=stage)
        if path == stage_parent / "stage":
            stage_parent.rename(held_parent)
            attacker_parent.mkdir()
            stage_parent.symlink_to(attacker_parent, target_is_directory=True)
        return descriptor, identity

    monkeypatch.setattr(BINDING, "_open_parent_descriptor", swap)

    with pytest.raises(BINDING.SourceBindingRejected, match="stage_path_changed"):
        BINDING.materialize_stage(
            archive_path=paths["archive"],
            receipt_path=paths["receipt"],
            stage_path=stage,
            expected_revision=revision,
        )

    assert not (held_parent / "stage").exists()
    assert not (attacker_parent / "stage").exists()


def test_materialize_rejects_mutated_or_hardlinked_archive(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    BINDING._remove_stage(paths["rebuild_stage"])
    os.chmod(paths["archive"], 0o600)
    paths["archive"].write_bytes(paths["archive"].read_bytes() + b"mutation")
    os.chmod(paths["archive"], 0o400)

    with pytest.raises(BINDING.SourceBindingRejected):
        BINDING.materialize_stage(
            archive_path=paths["archive"],
            receipt_path=paths["receipt"],
            stage_path=paths["rebuild_stage"],
            expected_revision=revision,
        )

    assert not paths["rebuild_stage"].exists()


@pytest.mark.parametrize("receipt_field", ["revision", "tree", "source_date_epoch"])
def test_downstream_verification_cryptographically_rejects_forged_source_claims(
    tmp_path: Path, receipt_field: str
) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    if receipt_field == "source_date_epoch":
        receipt["source"][receipt_field] += 1
    else:
        receipt["source"][receipt_field] = "1" * 40
    paths["receipt"].write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(BINDING.SourceBindingRejected):
        BINDING.verify_stage(
            archive_path=paths["archive"],
            stage_path=paths["stage"],
            receipt_path=paths["receipt"],
            expected_revision=revision,
        )


def test_bind_and_verify_two_byte_identical_builds_with_exclusive_manifest(tmp_path: Path) -> None:
    paths, receipt = _prepare_release(tmp_path)

    manifest = _bind(paths)

    assert manifest["source"] == receipt["source"]
    assert manifest["build"]["trial_count"] == 2
    assert manifest["build"]["artifacts_byte_identical"] is True
    assert manifest["build"]["wheel_from_sdist_content_parity"] is True
    assert [artifact["kind"] for artifact in manifest["artifacts"]] == ["sdist", "wheel"]
    assert stat.S_IMODE(paths["bound_dist"].stat().st_mode) == 0o500
    assert {path.name for path in paths["bound_dist"].iterdir()} == {
        artifact["filename"] for artifact in manifest["artifacts"]
    }
    assert (
        BINDING.verify_release(
            manifest_path=paths["manifest"],
            archive_path=paths["archive"],
            expected_revision=receipt["source"]["revision"],
            stage_path=paths["stage"],
            rebuild_stage_path=paths["rebuild_stage"],
            receipt_path=paths["receipt"],
            dist_path=paths["dist"],
            rebuild_dist_path=paths["rebuild_dist"],
            bound_dist_path=paths["bound_dist"],
            tool_lock_path=paths["stage"] / "uv.lock",
        )
        == manifest
    )
    assert (
        BINDING.verify_bound_release(
            manifest_path=paths["manifest"],
            archive_path=paths["archive"],
            expected_revision=receipt["source"]["revision"],
            receipt_path=paths["receipt"],
            bound_dist_path=paths["bound_dist"],
        )
        == manifest
    )
    with pytest.raises(BINDING.SourceBindingRejected, match="expected_revision"):
        BINDING.verify_bound_release(
            manifest_path=paths["manifest"],
            archive_path=paths["archive"],
            expected_revision="0" * 40,
            receipt_path=paths["receipt"],
            bound_dist_path=paths["bound_dist"],
        )
    BINDING._remove_stage(paths["bound_dist"])
    with pytest.raises(BINDING.SourceBindingRejected, match="output_exists"):
        _bind(paths)
    assert not paths["bound_dist"].exists()


def test_bind_rejects_rebuilt_artifact_with_different_bytes_even_when_contents_validate(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    _artifacts(paths["stage"], paths["dist"])
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"], stored_wheel=True)

    with pytest.raises(BINDING.SourceBindingRejected, match="rebuild_artifact_mismatch"):
        _bind(paths)

    assert not paths["manifest"].exists()


def test_bind_publishes_validated_bytes_when_primary_artifact_changes_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _receipt = _prepare_release(tmp_path)
    wheel = next(paths["dist"].glob("*.whl"))
    original_distribution_binding = BINDING._distribution_binding
    calls = 0

    def mutate_after_snapshot(**kwargs: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
        nonlocal calls
        result = original_distribution_binding(**kwargs)
        calls += 1
        if calls == 1:
            os.chmod(wheel, 0o600)
            wheel.write_bytes(wheel.read_bytes() + b"post-validation mutation")
        return result

    monkeypatch.setattr(BINDING, "_distribution_binding", mutate_after_snapshot)

    manifest = _bind(paths)

    wheel_record = next(artifact for artifact in manifest["artifacts"] if artifact["kind"] == "wheel")
    bound_wheel = paths["bound_dist"] / wheel_record["filename"]
    assert (
        BINDING._regular_file(bound_wheel, stage="bound", maximum=BINDING.MAX_ARCHIVE_BYTES, minimum=1)[0]
        != wheel.read_bytes()
    )
    assert "sha256:" + BINDING.hashlib.sha256(bound_wheel.read_bytes()).hexdigest() == wheel_record["sha256"]


@pytest.mark.parametrize("artifact_hazard", ["source-parity", "record"])
def test_bind_rejects_wheel_source_and_record_tampering(tmp_path: Path, artifact_hazard: str) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    kwargs: dict[str, Any] = {}
    if artifact_hazard == "source-parity":
        kwargs["source_override"] = ("algo_cli/main.py", b"malicious artifact\n")
    else:
        kwargs["corrupt_record"] = True
    _artifacts(paths["stage"], paths["dist"], **kwargs)
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"], **kwargs)

    with pytest.raises(BINDING.SourceBindingRejected):
        _bind(paths)

    assert not paths["manifest"].exists()


def test_bind_rejects_identically_injected_core_metadata(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    injected = b"Requires-Dist: malware>=1\n"
    _artifacts(paths["stage"], paths["dist"], injected_metadata=injected)
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"], injected_metadata=injected)

    with pytest.raises(BINDING.SourceBindingRejected, match="artifact_core_metadata"):
        _bind(paths)


def test_bind_rejects_non_hatchling_build_backend_before_artifact_claim(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path, build_backend="malicious.backend")
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    paths["dist"].mkdir()
    paths["rebuild_dist"].mkdir()

    with pytest.raises(BINDING.SourceBindingRejected, match="package_configuration"):
        _bind(paths)


def test_bind_rejects_non_regular_wheel_members(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    _artifacts(paths["stage"], paths["dist"], fifo_wheel_member="algo_cli/main.py")
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"], fifo_wheel_member="algo_cli/main.py")

    with pytest.raises(BINDING.SourceBindingRejected, match="wheel_type"):
        _bind(paths)


def test_bind_rejects_tool_lock_not_pinned_in_captured_source(tmp_path: Path) -> None:
    repository, revision = _repository(tmp_path, hatchling_version="1.30.0")
    paths = _paths(tmp_path)
    _capture(repository, revision, paths)
    _artifacts(paths["stage"], paths["dist"])
    _artifacts(paths["rebuild_stage"], paths["rebuild_dist"])

    with pytest.raises(BINDING.SourceBindingRejected, match="tool_lock_versions"):
        _bind(paths)


def test_verify_rejects_manifest_and_distribution_mutation(tmp_path: Path) -> None:
    paths, _receipt = _prepare_release(tmp_path)
    _bind(paths)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["build"]["trial_count"] = 3
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BINDING.SourceBindingRejected, match="manifest_mismatch"):
        BINDING.verify_release(
            manifest_path=paths["manifest"],
            archive_path=paths["archive"],
            expected_revision=_receipt["source"]["revision"],
            stage_path=paths["stage"],
            rebuild_stage_path=paths["rebuild_stage"],
            receipt_path=paths["receipt"],
            dist_path=paths["dist"],
            rebuild_dist_path=paths["rebuild_dist"],
            bound_dist_path=paths["bound_dist"],
            tool_lock_path=paths["stage"] / "uv.lock",
        )


def test_exclusive_write_and_archive_publish_roll_back_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output.json"

    original_fsync = BINDING.os.fsync
    calls = 0

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sensitive filesystem detail")
        original_fsync(descriptor)

    monkeypatch.setattr(BINDING.os, "fsync", fail_parent_fsync)
    with pytest.raises(BINDING.SourceBindingRejected, match="output_write"):
        BINDING._write_exclusive(output, b"{}\n")
    assert not output.exists()

    calls = 0
    destination = tmp_path / "published.tar"
    with pytest.raises(BINDING.SourceBindingRejected, match="archive_publish"):
        BINDING._publish_archive_payload(b"archive", destination)
    assert not destination.exists()


def test_cli_contract_is_absolute_and_failure_output_is_content_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, _revision = _repository(tmp_path)
    paths = _paths(tmp_path)
    secret = "not-a-revision-secret"

    result = BINDING.main(
        [
            "capture",
            "--repository",
            str(repository),
            "--revision",
            secret,
            "--archive",
            str(paths["archive"]),
            "--stage",
            str(paths["stage"]),
            "--receipt",
            str(paths["receipt"]),
        ]
    )

    output = capsys.readouterr().out
    assert result == 1
    assert secret not in output
    assert json.loads(output) == {"passed": False, "reason_code": "source_revision", "schema_version": 1}


def test_script_contains_materialize_and_two_build_cli_contract() -> None:
    parser = BINDING._parser()
    namespace = parser.parse_args(
        [
            "bind",
            "--archive",
            "/tmp/archive",
            "--expected-revision",
            "1" * 40,
            "--stage",
            "/tmp/stage-a",
            "--rebuild-stage",
            "/tmp/stage-b",
            "--receipt",
            "/tmp/receipt",
            "--dist",
            "/tmp/dist-a",
            "--rebuild-dist",
            "/tmp/dist-b",
            "--bound-dist",
            "/tmp/dist-bound",
            "--tool-lock",
            "/tmp/stage-a/uv.lock",
            "--manifest",
            "/tmp/manifest",
        ]
    )
    assert namespace.command == "bind"
    assert namespace.rebuild_stage == Path("/tmp/stage-b")
    assert namespace.rebuild_dist == Path("/tmp/dist-b")
    assert namespace.bound_dist == Path("/tmp/dist-bound")
    assert (
        parser.parse_args(
            [
                "materialize",
                "--archive",
                "/tmp/archive",
                "--expected-revision",
                "1" * 40,
                "--receipt",
                "/tmp/receipt",
                "--stage",
                "/tmp/stage",
            ]
        ).command
        == "materialize"
    )
    assert (
        parser.parse_args(
            [
                "verify-bound",
                "--archive",
                "/tmp/archive",
                "--expected-revision",
                "1" * 40,
                "--receipt",
                "/tmp/receipt",
                "--bound-dist",
                "/tmp/dist-bound",
                "--manifest",
                "/tmp/manifest",
            ]
        ).command
        == "verify-bound"
    )
