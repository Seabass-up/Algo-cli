"""Trusted stdlib-only bootstrap: set child limits, then enter Seatbelt before shell.

This file is invoked with -I -S, never imported as project code. No user code,
configuration, plugins or shell expansion runs before sandbox-exec.
"""
import json
import math
import os
import resource
import sys


def main() -> None:
    payload = json.loads(sys.argv[1])
    if set(payload) != {"profile", "command", "timeout", "uid_process_limit"}:
        raise ValueError("invalid contained launch")
    timeout = payload["timeout"]
    processes = payload["uid_process_limit"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("invalid deadline")
    if type(processes) is not int or not 1 <= processes <= 65536:
        raise ValueError("invalid process limit")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout) + 1, math.ceil(timeout) + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    old_hard = resource.getrlimit(resource.RLIMIT_NPROC)[1]
    ceiling = processes if old_hard == resource.RLIM_INFINITY else min(processes, old_hard)
    resource.setrlimit(resource.RLIMIT_NPROC, (ceiling, ceiling))
    os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", payload["profile"],
              "/bin/bash", "--noprofile", "--norc", "-c", payload["command"]], dict(os.environ))


if __name__ == "__main__":
    main()
