from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from algo_cli import ada_echo_veil_identity as identity


def _copy_qualified_tree(destination: Path) -> dict[str, bytes]:
    distribution = metadata.distribution("echo-veil")
    destination.mkdir(parents=True)
    for package_name in ("echo_veil", "echo_veil_origin"):
        shutil.copytree(
            Path(distribution.locate_file(package_name)),
            destination / package_name,
        )
    return {relative: (destination / relative).read_bytes() for relative in identity.QUALIFIED_ECHO_SOURCE_PATHS}


def test_qualified_snapshot_canonicalizes_windows_checkout_crlf(tmp_path: Path) -> None:
    root = tmp_path / "qualified-crlf"
    originals = _copy_qualified_tree(root)
    converted = 0
    for relative, payload in originals.items():
        canonical = payload.replace(b"\r\n", b"\n")
        windows_payload = canonical.replace(b"\n", b"\r\n")
        converted += windows_payload.count(b"\r\n")
        (root / relative).write_bytes(windows_payload)

    assert converted > 0
    snapshot = identity.capture_qualified_echo_source_tree(root)

    assert snapshot.tree_sha256 == identity.QUALIFIED_ECHO_SOURCE_TREE_SHA256
    assert dict(snapshot.files) == {
        relative: payload.replace(b"\r\n", b"\n") for relative, payload in originals.items()
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows hosted-workspace source binding contract")
def test_qualified_snapshot_pins_broad_ancestor_without_using_it_as_content_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "runner-workspace"
    root = parent / "site-packages"
    _copy_qualified_tree(root)
    system_root = Path(os.environ["SystemRoot"])
    icacls = system_root / "System32" / "icacls.exe"
    granted = subprocess.run(
        [str(icacls), str(parent), "/grant", "*S-1-5-20:(DC)"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert granted.returncode == 0, granted.stderr.decode(errors="replace")
    assert identity._windows_namespace_authorized(parent) is False

    original_read = identity._read_qualified_source_by_path
    displaced = parent / "displaced-site-packages"
    rename_was_blocked = False

    def read_while_trying_to_rebind(path: Path) -> bytes:
        nonlocal rename_was_blocked
        if not rename_was_blocked:
            with pytest.raises(OSError):
                root.rename(displaced)
            rename_was_blocked = True
        return original_read(path)

    monkeypatch.setattr(identity, "_read_qualified_source_by_path", read_while_trying_to_rebind)
    snapshot = identity.capture_qualified_echo_source_tree(root)

    assert rename_was_blocked is True
    assert snapshot.tree_sha256 == identity.QUALIFIED_ECHO_SOURCE_TREE_SHA256
    root.rename(displaced)
    assert displaced.is_dir()
