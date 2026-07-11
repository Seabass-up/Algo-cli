"""Regression checks for optional Rust harness indexer source."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUST_MAIN = ROOT / "harness-indexer" / "src" / "main.rs"


def _main_rs() -> str:
    return RUST_MAIN.read_text(encoding="utf-8")


def test_rust_indexer_unix_mtime_is_full_epoch_nanoseconds():
    source = _main_rs()

    assert "secs as u128 * 1_000_000_000" in source
    assert "+ metadata.mtime_nsec() as u128" in source


def test_rust_indexer_windows_mtime_converts_filetime_to_unix_epoch_nanoseconds():
    source = _main_rs()

    assert "WINDOWS_TO_UNIX_EPOCH_100NS" in source
    assert "saturating_sub(WINDOWS_TO_UNIX_EPOCH_100NS)" in source
    assert "* 100" in source
