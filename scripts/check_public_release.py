#!/usr/bin/env python3
"""Fail when public source or distribution artifacts contain private release residue."""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import json
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
GENERATED_PATH_PARTS = ("/node_modules/",)
ARTIFACT_FORBIDDEN_PATH_PARTS = ("/website/", "/node_modules/", "/.venv/", "/.git/")
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
ALLOWED_EMAIL_DOMAINS = {
    "algo-cli.com",
    "example.com",
    "example.test",
    "users.noreply.github.com",
}
LOCKFILE_BASENAMES = {"package-lock.json", "npm-shrinkwrap.json"}
SRI_RE = re.compile(
    r"^(?:(?:sha1|sha256|sha384|sha512)-[A-Za-z0-9+/]+={0,2})"
    r"(?:\s+(?:sha1|sha256|sha384|sha512)-[A-Za-z0-9+/]+={0,2})*$"
)
SRI_LENGTHS = {"sha1": 20, "sha256": 32, "sha384": 48, "sha512": 64}
INTEGRITY_FIELD_RE = re.compile(r'("integrity"\s*:\s*)("(?:\\.|[^"\\])*")')
DETECTOR_ASSIGNMENTS = {
    "PRIVATE_TERMS",
    "PRIVATE_FILENAME_PARTS",
    "DISALLOWED_SUFFIXES",
    "GENERATED_PATH_PARTS",
    "SECRET_PATTERNS",
    "MACHINE_PATH_PATTERNS",
    "EMAIL_RE",
    "ALLOWED_EMAIL_DOMAINS",
    "SRI_RE",
    "SRI_LENGTHS",
}


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
    if len(data) > TEXT_LIMIT:
        return None
    return data.decode("utf-8", errors="replace")


def _scan_name(name: str) -> list[str]:
    normalized = name.replace("\\", "/").lower()
    findings = []
    if any(part in normalized for part in PRIVATE_FILENAME_PARTS):
        findings.append("private filename")
    if normalized.endswith(DISALLOWED_SUFFIXES) or normalized.endswith(".ds_store"):
        findings.append("generated or credential-like filename")
    if any(part in f"/{normalized}" for part in GENERATED_PATH_PARTS):
        findings.append("generated dependency path")
    return findings


def _scan_artifact_name(name: str) -> list[str]:
    normalized_name = name.replace("\\", "/").lower()
    normalized = f"/{normalized_name}"
    return [
        f"{name}: forbidden distribution path"
        for part in ARTIFACT_FORBIDDEN_PATH_PARTS
        if part in normalized
    ]


def _mask_span(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines(keepends=True)
    for index in range(max(0, start_line - 1), min(len(lines), end_line)):
        lines[index] = "".join(char if char in "\r\n" else " " for char in lines[index])
    return "".join(lines)


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _mask_detector_definitions(name: str, text: str) -> str:
    if not name.replace("\\", "/").endswith("scripts/check_public_release.py"):
        return text
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    spans = sorted(
        {
            (node.lineno, node.end_lineno or node.lineno)
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and _assignment_names(node) & DETECTOR_ASSIGNMENTS
        },
        reverse=True,
    )
    for start_line, end_line in spans:
        text = _mask_span(text, start_line, end_line)
    return text


def _mask_lockfile_integrity(name: str, text: str) -> str:
    if Path(name.replace("!", "/")).name not in LOCKFILE_BASENAMES:
        return text
    try:
        json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text

    def replace(match: re.Match[str]) -> str:
        raw_value = match.group(2)
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            return match.group(0)
        if not isinstance(value, str) or not _valid_sri(value):
            return match.group(0)
        return f'{match.group(1)}"{" " * (len(raw_value) - 2)}"'

    return INTEGRITY_FIELD_RE.sub(replace, text)


def _valid_sri(value: str) -> bool:
    if SRI_RE.fullmatch(value) is None:
        return False
    for token in value.split():
        algorithm, separator, encoded = token.partition("-")
        if not separator or algorithm not in SRI_LENGTHS:
            return False
        try:
            digest = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return False
        if len(digest) != SRI_LENGTHS[algorithm]:
            return False
    return True


def _prepare_text_for_scan(name: str, text: str) -> str:
    return _mask_detector_definitions(name, _mask_lockfile_integrity(name, text))


def _scan_text(
    name: str,
    text: str,
    *,
    scan_private_terms: bool = True,
    scan_emails: bool = True,
) -> list[tuple[int, str]]:
    text = _prepare_text_for_scan(name, text)
    findings: list[tuple[int, str]] = []
    lowered = text.casefold()
    if scan_private_terms:
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
    if scan_emails:
        for match in EMAIL_RE.finditer(text):
            if match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS:
                findings.append((text.count("\n", 0, match.start()) + 1, "non-public email"))
    return sorted(set(findings))


def _scan_item(name: str, data: bytes, *, scan_content: bool = True) -> list[str]:
    findings = [f"{name}: {reason}" for reason in _scan_name(name)]
    if scan_content and len(data) > TEXT_LIMIT:
        findings.append(f"{name}: content exceeds {TEXT_LIMIT}-byte scan limit")
        return findings
    text = _decode(data) if scan_content else None
    if text is not None:
        binary = b"\0" in data[:4096]
        findings.extend(
            f"{name}:{line}: {reason}"
            for line, reason in _scan_text(
                name,
                text,
                scan_private_terms=not binary,
                scan_emails=not binary,
            )
        )
    return findings


def scan_repository() -> list[str]:
    findings: list[str] = []
    for path in _candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        with path.open("rb") as handle:
            findings.extend(_scan_item(relative, handle.read(TEXT_LIMIT + 1)))
    return findings


def scan_archive(path: Path) -> list[str]:
    findings: list[str] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                findings.extend(_scan_artifact_name(f"{path.name}!{info.filename}"))
                with archive.open(info) as handle:
                    data = handle.read(TEXT_LIMIT + 1)
                findings.extend(_scan_item(f"{path.name}!{info.filename}", data))
        return findings
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    findings.extend(_scan_artifact_name(f"{path.name}!{member.name}"))
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
