---
title: Continuum Memory
category: memory
updated: 2026-09-21
last_reviewed: 2026-09-21
---

# Continuum Memory

Continuum is Algo's supported governed memory service. It is independent of
retired memory systems. The integration owner is `algo_cli/continuum_memory.py`;
model tools are defined in `algo_cli/continuum_tools.py`.

## Configuration and recovery

Install the native `continuum-memory` command in the operator's executable PATH
and explicitly set `continuum_enabled` to `true` in Algo's configuration. Algo
invokes it directly with project `legacy-memory`, harness `algo`, and an explicit
`shared` or `private` scope. The store is the native CLI default,
`~/Library/Application Support/ContinuumMemory`. The command is a separately
installed service; Algo does not bundle it or initialize a namespace silently.

`/memory doctor` verifies both scopes. Native `memory_status` reports conflicts,
stale dependencies and unusable critical state. Prompt assembly obtains bounded
context in both scopes and validates the packets before use. Required records,
provenance, omitted counts and policy-withheld counts remain in the packets.
Memory bodies are untrusted context, never permissions or proof of current facts.

Prompt context normally requests 12,000 serialized bytes for shared state and
6,000 for private state. If Continuum reports that required records and their
verification metadata exceed that budget, Algo retries that scope once with a
larger packet, capped at 32,768 bytes. It validates the backend's numeric size
report and retains native packet verification. Integrity, policy, stale-context,
and critical-state refusals are not size retries; no required records are dropped.
A refused display estimate shows unavailable context rather than terminating the
REPL. Model execution still requires a successfully verified context packet.

Unreadable, malformed or retired-only memory configuration blocks startup before
model execution. It does not select a replacement backend or load plaintext
memory. Repair the original config, explicitly select Continuum, and verify the
native service. Unrelated settings and historical stores must be preserved.
Retired keys are discarded when a valid explicit Continuum configuration is saved.

## Operations and limitations

The fifteen `memory_*` tools expose the native verification, status, evidence,
typed state, context, revocation, conflict resolution and handoff operations.
Every call binds its scope. Writes use expected revisions and stable request IDs;
an uncertain write requires reconciliation, not a blind retry. `/remember` and
`append_lesson` write to Algo's existing private Continuum fact record and verify
the resulting revision. No plaintext shadow is created.

Encryption at rest is not isolation from a hostile process running as the same
OS user. The protected-root policy denies raw access through typed file, Git,
PDF and vision routes and prevents plaintext profile writes. Arbitrary shell and
the unqualified Cobalt browser routes remain unavailable while protected memory
is selected because those processes do not provide qualified containment.
Ordinary typed file work outside protected roots remains available.

Model-invoked broad slash commands are limited to reviewed navigation, status,
mode, native memory and protected harness routes. Authentication, callback-file,
cloud file-upload and other unqualified slash operations cannot hide behind that
wrapper. Git observations and automatic evidence refuse repositories enclosing
protected roots, and do not invoke text conversion or filesystem-monitor hooks.
Parent listings omit protected entries; missing-file recovery does not recursively
search protected runs. This is route enforcement, not hostile same-user isolation.
Path validation inspects components before collapsing `..`. Protected text reads
pin the parent directory and file descriptor and recheck identity before returning
content. Git inventory validation refuses hardlinked files, aliased metadata and
redirected worktrees before aggregate reads, including whitespace-bearing names.

Auxiliary history uses content-free keyed receipts. Runtime refusal, missing
credentials, stale context or integrity failures must remain visible and must
not trigger another memory backend.

## Verification

Run `.venv/bin/python -B -m pytest tests/test_continuum_memory.py
tests/test_continuum_tools.py tests/test_protected_memory_preflight.py
tests/test_irene_memory_path_policy.py` from the source checkout. These tests
exercise native routing, revision-bound writes, explicit scopes, invalid inputs,
fail-closed startup and protected-path denial with ordinary workspace controls.
Unit tests alone do not qualify an installed service, a public upgrade, or hosted
Windows, macOS and Linux delivery. Those require separate current-source evidence.
The public retrieval corpus has successor schema `algo-grounded-retrieval-v3`:
two retired-backend labels now refer to this native contract. Historical v2
reports are preserved and do not qualify the changed source or labels.
