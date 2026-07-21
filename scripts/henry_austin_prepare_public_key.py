#!/usr/bin/env python3
"""Materialize one protected Austin public key for signed qualification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping, NoReturn


ENVIRONMENT_KEY = "AUSTIN_DISABLED_AUTHORITY_PUBLIC_KEY_BASE64URL"
OUTPUT_NAME = "AustinDisabledNativeAuthority.bin"
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class AustinKeyPreparationRejected(RuntimeError):
    """A content-free public-key preparation invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_key_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise AustinKeyPreparationRejected(reason_code)


def _decode_key(value: object) -> bytes:
    if type(value) is not str or _BASE64URL_RE.fullmatch(value) is None:
        _reject("austin_key_encoding")
    try:
        decoded = base64.b64decode(
            value.replace("-", "+").replace("_", "/") + "=",
            altchars=None,
            validate=True,
        )
    except (ValueError, TypeError):
        _reject("austin_key_encoding")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != 32 or canonical != value:
        _reject("austin_key_encoding")
    return decoded


def _assert_protected_runner(environment: Mapping[str, str]) -> Path:
    if (
        type(environment) is not dict
        or environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("GITHUB_REF_PROTECTED") != "true"
        or environment.get("RUNNER_ENVIRONMENT") != "self-hosted"
        or environment.get("RUNNER_OS") != "macOS"
        or environment.get("RUNNER_ARCH") != "ARM64"
        or sys.platform != "darwin"
    ):
        _reject("austin_key_runner")
    raw = environment.get("RUNNER_TEMP")
    if type(raw) is not str or not raw or "\x00" in raw:
        _reject("austin_key_output")
    temporary = Path(raw)
    if not temporary.is_absolute() or ".." in temporary.parts:
        _reject("austin_key_output")
    try:
        resolved = temporary.resolve(strict=True)
        info = temporary.lstat()
    except OSError:
        _reject("austin_key_output")
    if (
        resolved != temporary
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_mode & 0o022
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        _reject("austin_key_output")
    return resolved


def prepare_public_key(
    *,
    output: Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    temporary = _assert_protected_runner(environment)
    if (
        not isinstance(output, Path)
        or not output.is_absolute()
        or output.parent != temporary
        or output.name != OUTPUT_NAME
        or output.exists()
        or output.is_symlink()
    ):
        _reject("austin_key_output")
    payload = _decode_key(environment.get(ENVIRONMENT_KEY))
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created = False
    committed = False
    try:
        descriptor = os.open(output, flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _reject("austin_key_write")
            view = view[written:]
        os.fsync(descriptor)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_size != 32
        ):
            _reject("austin_key_write")
        os.close(descriptor)
        descriptor = None
        directory = os.open(
            temporary,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        committed = True
    except AustinKeyPreparationRejected:
        raise
    except OSError:
        _reject("austin_key_write")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not committed:
            try:
                output.unlink()
            except OSError:
                pass
    return {
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "status": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = prepare_public_key(
            output=arguments.output.absolute(),
            environment=dict(os.environ),
        )
    except AustinKeyPreparationRejected as error:
        print(
            json.dumps(
                {"reason_code": error.reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
