#!/usr/bin/env python3
"""Fail when public source or distribution artifacts contain private release residue."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
TEXT_LIMIT = 4_000_000

PRIVATE_TERMS = (
    "sco" + "tt",
    "whit" + "lock",
    "x" + "ds",
    "char" + "lie",
    "lodge of " + "overland park",
    "xds" + "electric",
    "arts" + " block",
    "long" + " electric",
    "05_" + "software_tools",
    "cloud" + "storage",
    "googledrive-",
)
PRIVATE_FILENAME_PARTS = (
    "personal/",
    "xds",
    "charlie",
    "lodge",
)
DISALLOWED_SUFFIXES = (".bak", ".pyc", ".p12", ".pfx", ".pem", ".key")
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
MACHINE_PATH_PATTERNS = (
    re.compile(r"\bseaba\b", re.IGNORECASE),
    re.compile(r"/Users/(?!example(?:/|\b)|demo(?:/|\b)|me(?:/|\b))[^/\s`\"']+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!example(?:[\\/]|\b)|demo(?:[\\/]|\b))[^\\/\s`\"']+", re.IGNORECASE),
    re.compile(
        r"[A-Za-z]:(?:\\\\|/)Users(?:\\\\|/)(?!example(?:\\\\|/|\b)|demo(?:\\\\|/|\b))[^\\/\s`\"']+",
        re.IGNORECASE,
    ),
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.test", "users.noreply.github.com"}


def _candidate_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        path = ROOT / raw.decode("utf-8", errors="surrogateescape")
        if path.is_file():
            paths.append(path)
    return sorted(paths)


def _decode(data: bytes) -> str | None:
    if len(data) > TEXT_LIMIT or b"\0" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_name(name: str) -> list[str]:
    normalized = name.replace("\\", "/").lower()
    findings = []
    if any(part in normalized for part in PRIVATE_FILENAME_PARTS):
        findings.append("private filename")
    if normalized.endswith(DISALLOWED_SUFFIXES) or normalized.endswith(".ds_store"):
        findings.append("generated or credential-like filename")
    return findings


def _scan_text(name: str, text: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    lowered = text.casefold()
    for term in PRIVATE_TERMS:
        start = 0
        while True:
            offset = lowered.find(term, start)
            if offset < 0:
                break
            findings.append((text.count("\n", 0, offset) + 1, "private marker"))
            start = offset + len(term)
    for pattern in (*SECRET_PATTERNS, *MACHINE_PATH_PATTERNS):
        for match in pattern.finditer(text):
            findings.append((text.count("\n", 0, match.start()) + 1, "secret or machine path"))
    for match in EMAIL_RE.finditer(text):
        if match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS:
            findings.append((text.count("\n", 0, match.start()) + 1, "non-example email"))
    return sorted(set(findings))


def _scan_item(name: str, data: bytes, *, scan_content: bool = True) -> list[str]:
    findings = [f"{name}: {reason}" for reason in _scan_name(name)]
    text = _decode(data) if scan_content else None
    if text is not None:
        findings.extend(f"{name}:{line}: {reason}" for line, reason in _scan_text(name, text))
    return findings


def scan_repository() -> list[str]:
    findings: list[str] = []
    for path in _candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        findings.extend(_scan_item(relative, path.read_bytes(), scan_content=path.resolve() != SELF))
    return findings


def scan_archive(path: Path) -> list[str]:
    findings: list[str] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                findings.extend(_scan_item(f"{path.name}!{info.filename}", archive.read(info)))
        return findings
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    findings.extend(_scan_item(f"{path.name}!{member.name}", handle.read(TEXT_LIMIT + 1)))
    except tarfile.TarError:
        findings.append(f"{path}: unsupported artifact format")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", default=[], help="Wheel or sdist to inspect")
    parser.add_argument("--artifacts-only", action="store_true", help="Skip repository source scan")
    args = parser.parse_args(argv)

    findings = [] if args.artifacts_only else scan_repository()
    for value in args.artifact:
        findings.extend(scan_archive(Path(value)))
    if findings:
        print("Public-release scan failed:", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Public-release scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
