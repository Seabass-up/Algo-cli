#!/usr/bin/env python3
"""Reject private metadata and content anywhere in reachable Git history."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_RELEASE_SCAN_PATH = ROOT / "scripts" / "check_public_release.py"
_RELEASE_SCAN_SPEC = importlib.util.spec_from_file_location(
    "algo_cli_public_release_scan",
    _RELEASE_SCAN_PATH,
)
if _RELEASE_SCAN_SPEC is None or _RELEASE_SCAN_SPEC.loader is None:
    raise RuntimeError("public_release_scanner_unavailable")
check_public_release = importlib.util.module_from_spec(_RELEASE_SCAN_SPEC)
_RELEASE_SCAN_SPEC.loader.exec_module(check_public_release)


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def _git(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode("utf-8", errors="replace")


def _display_path(path: str) -> str:
    return path.replace("\r", "\\r").replace("\n", "\\n")


def _scan_text(
    label: str,
    text: str,
    *,
    scan_private_terms: bool = True,
    scan_emails: bool = True,
) -> set[str]:
    return {
        f"{label}:{line}: {reason}"
        for line, reason in check_public_release._scan_text(
            label,
            text,
            scan_private_terms=scan_private_terms,
            scan_emails=scan_emails,
        )
    }


def _object_message(raw: bytes, identity_headers: tuple[bytes, ...]) -> str:
    header, _separator, message = raw.partition(b"\n\n")
    selected = [
        line
        for line in header.splitlines()
        if any(line.startswith(prefix) for prefix in identity_headers)
    ]
    return b"\n".join([*selected, message]).decode("utf-8", errors="replace")


def _tree_from_commit(raw: bytes) -> str | None:
    first_line = raw.partition(b"\n")[0]
    if not first_line.startswith(b"tree "):
        return None
    return first_line.removeprefix(b"tree ").decode("ascii", errors="ignore") or None


def _scan_tree(
    root: Path,
    tree: str,
    seen_blobs: set[str],
    blob_sizes: dict[str, int],
) -> set[str]:
    findings: set[str] = set()
    output = _git_bytes(root, "ls-tree", "-r", "-z", tree)
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            continue
        fields = metadata.split(b" ", 2)
        if len(fields) != 3 or fields[1] != b"blob":
            continue
        object_id = fields[2].decode("ascii", errors="ignore")
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if not object_id:
            continue
        display = _display_path(path)
        label = f"history:{object_id[:12]}:{display}"
        findings.update(f"{label}: {reason}" for reason in check_public_release._scan_name(display))
        if object_id in seen_blobs:
            continue
        seen_blobs.add(object_id)
        size = blob_sizes.get(object_id)
        if size is None:
            size = int(_git(root, "cat-file", "-s", object_id).strip())
            blob_sizes[object_id] = size
        if size > check_public_release.TEXT_LIMIT:
            findings.add(
                f"history:{object_id[:12]}: content exceeds "
                f"{check_public_release.TEXT_LIMIT}-byte scan limit"
            )
            continue
        data = _git_bytes(root, "cat-file", "blob", object_id)
        findings.update(check_public_release._scan_item(label, data))
    return findings


def _reachable_objects(root: Path) -> dict[str, tuple[str, int]]:
    object_ids = sorted(
        {
            line
            for line in _git(root, "rev-list", "--objects", "--all", "--no-object-names").splitlines()
            if line
        }
    )
    if not object_ids:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=root,
        check=True,
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        capture_output=True,
    )
    objects: dict[str, tuple[str, int]] = {}
    for row in proc.stdout.splitlines():
        fields = row.split(b" ", 2)
        if len(fields) != 3:
            continue
        raw_object_id, raw_type, raw_size = fields
        try:
            objects[raw_object_id.decode("ascii")] = (
                raw_type.decode("ascii"),
                int(raw_size),
            )
        except (UnicodeDecodeError, ValueError):
            continue
    if set(objects) != set(object_ids):
        raise RuntimeError("Git did not classify every reachable object")
    return objects


def _scan_refs_and_tags(
    root: Path,
    *,
    allow_contributor_identities: bool,
) -> tuple[set[str], set[str], set[str]]:
    findings: set[str] = set()
    root_trees: set[str] = set()
    root_blobs: set[str] = set()
    ref_rows = _git_bytes(
        root,
        "for-each-ref",
        "--format=%(refname)%00%(objecttype)%00%(objectname)%00%(*objecttype)%00%(*objectname)",
    )
    for row in ref_rows.splitlines():
        fields = row.split(b"\0")
        if len(fields) != 5:
            continue
        ref, object_type, object_id, peeled_type, peeled_id = fields
        ref_text = ref.decode("utf-8", errors="surrogateescape")
        findings.update(_scan_text(f"ref:{_display_path(ref_text)}", ref_text, scan_emails=False))
        target_type = peeled_type or object_type
        target_id = peeled_id or object_id
        decoded_id = target_id.decode("ascii", errors="ignore")
        if target_type == b"tree" and decoded_id:
            root_trees.add(decoded_id)
        elif target_type == b"blob" and decoded_id:
            root_blobs.add(decoded_id)

    tag_rows = _git_bytes(
        root,
        "for-each-ref",
        "--format=%(objecttype)%00%(objectname)",
        "refs/tags",
    )
    for row in tag_rows.splitlines():
        object_type, separator, raw_object_id = row.partition(b"\0")
        if not separator or object_type != b"tag":
            continue
        object_id = raw_object_id.decode("ascii", errors="ignore")
        if not object_id:
            continue
        raw_tag = _git_bytes(root, "cat-file", "tag", object_id)
        text = _object_message(raw_tag, (b"tagger ",))
        findings.update(
            _scan_text(
                f"tag:{object_id[:12]}",
                text,
                scan_private_terms=not allow_contributor_identities,
                scan_emails=not allow_contributor_identities,
            )
        )
    return findings, root_trees, root_blobs


def scan_history(
    root: Path = ROOT,
    *,
    allow_contributor_identities: bool = False,
) -> list[str]:
    if _git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        return ["repository is shallow; fetch complete history before publication"]

    findings: set[str] = set()
    trees: set[str] = set()
    reachable_objects = _reachable_objects(root)
    blob_sizes = {
        object_id: size
        for object_id, (object_type, size) in reachable_objects.items()
        if object_type == "blob"
    }
    commits = [line for line in _git(root, "rev-list", "--all").splitlines() if line]
    for commit in commits:
        raw_commit = _git_bytes(root, "cat-file", "commit", commit)
        tree = _tree_from_commit(raw_commit)
        if tree:
            trees.add(tree)
        text = _object_message(raw_commit, (b"author ", b"committer "))
        findings.update(
            _scan_text(
                f"commit:{commit[:12]}",
                text,
                scan_private_terms=not allow_contributor_identities,
                scan_emails=not allow_contributor_identities,
            )
        )

    ref_findings, ref_trees, ref_blobs = _scan_refs_and_tags(
        root,
        allow_contributor_identities=allow_contributor_identities,
    )
    findings.update(ref_findings)
    trees.update(ref_trees)
    blob_sizes.update(
        {
            object_id: int(_git(root, "cat-file", "-s", object_id).strip())
            for object_id in ref_blobs
            if object_id not in blob_sizes
        }
    )

    seen_blobs: set[str] = set()
    for tree in sorted(trees):
        findings.update(_scan_tree(root, tree, seen_blobs, blob_sizes))

    for object_id, size in sorted(blob_sizes.items()):
        if object_id in seen_blobs:
            continue
        label = f"history:{object_id[:12]}:unpathed-blob"
        if size > check_public_release.TEXT_LIMIT:
            findings.add(
                f"{label}: content exceeds {check_public_release.TEXT_LIMIT}-byte scan limit"
            )
            continue
        data = _git_bytes(root, "cat-file", "blob", object_id)
        findings.update(check_public_release._scan_item(label, data))
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-contributor-identities",
        action="store_true",
        help="Allow public contributor names and emails in commit and tag metadata",
    )
    args = parser.parse_args(argv)
    findings = scan_history(allow_contributor_identities=args.allow_contributor_identities)
    if findings:
        print("Public-history scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        print("Publish from a reviewed squashed/orphan history or a new public repository.", file=sys.stderr)
        return 1
    print("Public-history content and metadata scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
