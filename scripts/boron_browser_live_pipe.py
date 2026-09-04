#!/usr/bin/env python3
"""Verify the real Chromium ASCIIZ fd-3/fd-4 DevTools pipe on macOS.

This is transport evidence only.  It uses an automatically deleted profile and
does not navigate, enable the public route, or claim the locally installed
Chrome is within the production security-update window.
"""

from __future__ import annotations

import hashlib
import json
import fcntl
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time

from algo_cli.boron_browser_wrapper import (
    BoronPidProcess,
    BoronPipeDecoder,
    encode_boron_pipe_message,
)


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
VERSION_RE = re.compile(
    r"^Google Chrome ([1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3})[ \t]*\n?$"
)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeError("pipe_write")
        view = view[written:]


def main() -> int:
    if not CHROME.is_file():
        raise RuntimeError("chrome_not_installed")
    version_result = subprocess.run(
        [str(CHROME), "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    match = VERSION_RE.fullmatch(version_result.stdout)
    if version_result.returncode != 0 or match is None:
        raise RuntimeError("chrome_version")
    expected_version = match.group(1)

    controller_to_chrome_read, controller_to_chrome_write = os.pipe()
    chrome_to_controller_read, chrome_to_controller_write = os.pipe()
    null_fd = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
    child_read_fd = fcntl.fcntl(
        controller_to_chrome_read,
        fcntl.F_DUPFD_CLOEXEC,
        10,
    )
    child_write_fd = fcntl.fcntl(
        chrome_to_controller_write,
        fcntl.F_DUPFD_CLOEXEC,
        10,
    )
    sources = {
        controller_to_chrome_read,
        controller_to_chrome_write,
        chrome_to_controller_read,
        chrome_to_controller_write,
        null_fd,
        child_read_fd,
        child_write_fd,
    }
    file_actions: list[tuple[int, ...]] = [
        (os.POSIX_SPAWN_DUP2, null_fd, 0),
        (os.POSIX_SPAWN_DUP2, null_fd, 1),
        (os.POSIX_SPAWN_DUP2, null_fd, 2),
        (os.POSIX_SPAWN_DUP2, child_read_fd, 3),
        (os.POSIX_SPAWN_DUP2, child_write_fd, 4),
    ]
    for descriptor in sorted(sources - {3, 4}):
        file_actions.append((os.POSIX_SPAWN_CLOSE, descriptor))

    process: BoronPidProcess | None = None
    decoder = BoronPipeDecoder()
    with tempfile.TemporaryDirectory(prefix="boron-live-pipe-") as profile:
        argv = [
            str(CHROME),
            "--headless=new",
            "--remote-debugging-pipe=JSON",
            f"--user-data-dir={profile}",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "about:blank",
        ]
        try:
            pid = os.posix_spawn(
                str(CHROME),
                argv,
                {
                    "HOME": profile,
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "TZ": "UTC",
                },
                file_actions=file_actions,
                setsid=True,
            )
            process = BoronPidProcess(pid)
            os.close(controller_to_chrome_read)
            os.close(chrome_to_controller_write)
            os.close(null_fd)
            os.close(child_read_fd)
            os.close(child_write_fd)
            _write_all(
                controller_to_chrome_write,
                encode_boron_pipe_message(
                    {"id": 1, "method": "Browser.getVersion", "params": {}}
                ),
            )
            deadline = time.monotonic() + 15
            message: dict | None = None
            while time.monotonic() < deadline and message is None:
                ready, _, _ = select.select(
                    [chrome_to_controller_read],
                    [],
                    [],
                    max(0, deadline - time.monotonic()),
                )
                if not ready:
                    break
                chunk = os.read(chrome_to_controller_read, 65_536)
                if not chunk:
                    break
                messages = decoder.feed(chunk)
                if messages:
                    if len(messages) != 1:
                        raise RuntimeError("pipe_message_count")
                    message = messages[0]
            if message is None:
                raise RuntimeError("pipe_timeout")
            if frozenset(message) != {"id", "result"} or message["id"] != 1:
                raise RuntimeError("pipe_response_schema")
            result = message["result"]
            if type(result) is not dict:
                raise RuntimeError("pipe_response_result")
            product = result.get("product")
            protocol = result.get("protocolVersion")
            if product not in {
                f"Chrome/{expected_version}",
                f"HeadlessChrome/{expected_version}",
            }:
                raise RuntimeError("pipe_browser_identity")
            if protocol != "1.3":
                raise RuntimeError("pipe_protocol")
            decoder.finish()
        finally:
            for descriptor in (controller_to_chrome_write, chrome_to_controller_read):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3.0)

    row = {
        "schema_version": 1,
        "result": "pass",
        "scope": "real stable Chrome ASCIIZ JSON over inherited fds 3 and 4",
        "limitations": "local macOS transport-only probe; no navigation, broker, Docker image, or freshness claim",
        "browser_major": int(expected_version.split(".", 1)[0]),
        "protocol_version": protocol,
        "command_count": 1,
        "response_count": 1,
        "remote_debugging_tcp": False,
        "ephemeral_profile_removed": True,
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    print(json.dumps({**row, "evidence_digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"result": "fail", "reason_code": str(error)}, sort_keys=True))
        raise SystemExit(1) from None
