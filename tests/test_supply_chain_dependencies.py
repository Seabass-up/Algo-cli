"""Keep audit tooling modern without dropping the CLI's Python 3.10 contract."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version
import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def document(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def test_cli_and_regular_tests_still_support_python310_without_audit_packages():
    project = document("pyproject.toml")["project"]
    assert Version("3.10") in SpecifierSet(project["requires-python"])
    extras = project["optional-dependencies"]
    for requirement in [*project["dependencies"], *extras["dev"]]:
        assert Requirement(requirement).name not in {"filelock", "pip-audit"}
    assert "supply-chain" not in extras


def test_audit_group_explicitly_requires_modern_python_and_patched_filelock():
    config = document("pyproject.toml")
    requirements = {Requirement(raw).name: Requirement(raw) for raw in config["dependency-groups"]["supply-chain"]}
    versions = SpecifierSet(config["tool"]["uv"]["dependency-groups"]["supply-chain"]["requires-python"])
    assert Version("3.10") not in versions and Version("3.11") in versions
    assert requirements["pip-audit"].specifier == SpecifierSet("==2.10.1")
    patched = requirements["filelock"]
    assert patched.marker is None
    assert Version("4.2.1") in patched.specifier
    assert Version("3.20.3") not in patched.specifier
    assert Version("3.31.0") not in patched.specifier
    assert Version("4.2.0") not in patched.specifier


def test_lock_has_no_vulnerable_compatibility_branch_and_binds_audit_group():
    lock = document("uv.lock")
    packages = lock["package"]
    filelocks = [item for item in packages if item["name"] == "filelock"]
    assert filelocks and all(Version(item["version"]) >= Version("4.2.1") for item in filelocks)
    msgpacks = [item for item in packages if item["name"] == "msgpack"]
    assert msgpacks and all(Version(item["version"]) >= Version("1.2.3") for item in msgpacks)
    project = next(item for item in packages if item["name"] == "algo-cli-runtime")
    group = project["dev-dependencies"]["supply-chain"]
    assert {item["name"] for item in group} == {"filelock", "pip-audit"}
    assert "supply-chain" not in project.get("optional-dependencies", {})


@pytest.mark.parametrize("workflow", ["oliver-ci.yml", "oliver-release.yml"])
def test_every_active_audit_workflow_uses_the_locked_group(workflow: str):
    text = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
    assert "--extra supply-chain" not in text
    assert "--group supply-chain" in text
    assert "--require-hashes --disable-pip --strict" in text
    assert "--ignore-vuln" not in text


def test_actual_cachecontrol_consumer_roundtrip_and_delete(tmp_path):
    caches = pytest.importorskip("cachecontrol.caches")
    cache = caches.FileCache(str(tmp_path / "http"))
    cache.set("synthetic-key", b"literal cached data")
    assert cache.get("synthetic-key") == b"literal cached data"
    cache.delete("synthetic-key")
    assert cache.get("synthetic-key") is None


def test_patched_msgpack_roundtrip_retains_strict_map_keys():
    msgpack = pytest.importorskip("msgpack")
    assert Version(msgpack.__version__) >= Version("1.2.3")
    data = {"fixture": [1, 2], "value": b"public synthetic cache data"}
    assert msgpack.unpackb(msgpack.packb(data, use_bin_type=True)) == data
    with pytest.raises(ValueError, match="map_key"):
        msgpack.unpackb(msgpack.packb({42: b"invalid key fixture"}, use_bin_type=True))


def test_actual_cachecontrol_write_cannot_cross_another_process_lock(tmp_path):
    caches = pytest.importorskip("cachecontrol.caches")
    filelock = pytest.importorskip("filelock")
    cache = caches.FileCache(str(tmp_path / "http"))
    path = Path(cache._fn("synthetic-key"))
    path.parent.mkdir(parents=True)
    command = (
        "import sys;from functools import partial;from filelock import FileLock,Timeout;"
        "from cachecontrol.caches import FileCache;"
        "cache=FileCache(sys.argv[1],lock_class=partial(FileLock,timeout=0.2));"
        "\ntry: cache.set('synthetic-key',b'must not publish')"
        "\nexcept Timeout: print('blocked')"
        "\nelse: raise SystemExit('write crossed held lock')"
    )
    with filelock.FileLock(str(path) + ".lock", timeout=5):
        contender = subprocess.run([sys.executable, "-I", "-c", command, str(tmp_path / "http")],
                                   capture_output=True, text=True, timeout=15)
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip() == "blocked"
        assert not path.exists()
    cache.set("synthetic-key", b"published after release")
    assert cache.get("synthetic-key") == b"published after release"
