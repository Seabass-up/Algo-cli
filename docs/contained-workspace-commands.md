# Commands, builds and tests with protected Continuum

`run_shell` no longer has a blanket Continuum prohibition on the qualified macOS
runtime. It uses an OS-contained workspace runner when `continuum_enabled` is
true. **Memory protection stays enabled.** Existing policy, workspace binding,
safe mode, destructive-command checks and exact-action approval still apply.
A live user-entered YOLO activation preapproves eligible workspace commands;
it does not disable these protections or give child agents the same activation.

## What works

Shell commands and subprocesses execute in the actual selected project directory,
so builds can produce workspace artifacts and tests can exercise current edits.
Positive checks include real Python tests (passing and failing), npm builds, C
compilation, and YOLO execution. The original Algo checkout's 45 adapter tests
also ran through the contained runner with memory still selected:

```
PYTHONPATH=. uv run --no-sync --offline pytest tests/test_ssea.py -q
```

Use installed project dependencies. Clean isolated caches, offline package-manager
settings and network denial mean automatic dependency acquisition is not assumed.
`uv run --no-sync --offline ...` avoids an implicit environment reinstall. Missing
dependencies are a real setup blocker, not a reason to disable memory protection.
The shell returns actual exit status; a failing test stays failed. Readback,
metadata discovery and source substring checks do not prove execution.

## Enforced boundary

- Explicit nonprotected, unaliased project directory; home/system-root execution
  is refused. Runtime-injected configuration, cwd, safe mode and cancellation
  cannot be overridden by model arguments.
- macOS Seatbelt `deny default`: writes only to the project and private scratch;
  reads additionally cover closed system/tool runtime locations. Arbitrary PATH
  entries are not permission to read personal directories.
- Protected memory roots are explicit read/write/exec denials, including their
  configured/resolved identities. No plaintext memory fallback/shadow is created.
- Known credential/private-key files, sensitive directories and VCS internals
  are denied even within the workspace, with case-insensitive patterns. Use typed
  Git tools for admitted VCS observations. Synthetic data in private scratch is
  allowed; symlink escapes still resolve against the outside denial.
- A bounded, metadata-only, nofollow `find` inventory detects pre-existing
  hardlinks/special files and known protected inode aliases. They and their
  renamable ancestors receive denial rules. New hardlink creation is denied.
  Inventory time counts against the command deadline; large workspaces have
  measurable setup cost. Incomplete inventory fails closed.
- Clean child HOME, environment, configuration and inherited descriptors. No
  parent credentials, auth sockets or keychain access. The installed public CA
  certificate bundle is copied to scratch for HTTP-client initialization; this
  does not grant access to arbitrary PEM/private-key files or enable network.
- Network (including loopback/Unix services), Mach lookup, privileged helpers and
  process-session/group escape are denied. Parent process environments cannot be
  inspected. Hardware/OS sysctls are separate from denied process-argument access.
- Ordinary subprocesses are supported. Children cannot detach using setsid or
  setpgid. Only same-sandbox signals are allowed. The process group is killed
  even when its leader exits successfully, so background services are not kept.
  The leader is reaped; an orphan may briefly remain an init-owned zombie.
- Output is bounded to 64 KiB (stderr capture 4 KiB); overflow is not a successful
  verifier. Existing 120-second maximum and caller cancellation apply. A monitor
  stops groups exceeding 32 observed processes. An inherited hard NPROC ceiling
  also constrains the child's total UID process count to admission count +32;
  this is **not a kernel per-tree cgroup quota**. CPU, 256 MiB per-file size,
  256 descriptors and zero core-dump limits are inherited by descendants.
  No aggregate RSS/disk quota is claimed.

The stdlib-only bootstrap applies resource limits and then execs `sandbox-exec`
before interpreting the command. It uses no `preexec_fn` in the multithreaded
agent. If containment fails, there is **no uncontained fallback**.

## Limits that remain real

This does not authorize network access, account authentication, dependency
installation, credential use, persistent background daemons, privileged commands,
or protected-store access. Projects requiring network test fixtures or personal
configuration may need a separately qualified typed workflow. Safe mode can still
block mutations. Other hosts fail closed until their boundary is qualified.
A shell/test/build command is delegated code execution with workspace write
access, not a guarantee that arbitrary project code is harmless. Known destructive
command checks remain, but they are not a complete static analysis of scripts.
Hostile concurrent processes running as the same OS account remain outside the
cooperative memory-isolation guarantee; preflight is not a filesystem lock.

## Delivery and evidence

Source candidate: `~/Code/Algo-cli-contained-shell-20261117`, preserving the prior
qualified 0.20.4 SSEA, CLI-Anything, QC and private speech implementations.

Qualification/activation receipts:
`~/.local/share/algo-cli/qualification/contained-shell-20261117/`

The installed runtime's `LOCAL-RUNTIME.json` is authoritative for activation.
Existing Algo processes must restart to load new code and the corrected YOLO
prompt. No saved memory selection or YOLO preference needs changing. Web/Google
readiness and authentication are separate; verification and privacy safeguards
were not removed simply to change a session warning.
