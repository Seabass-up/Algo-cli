---
title: Echo Veil Security Status
description: Authoritative adapter, entry-point classification, and promotion gate for protected Algo CLI memory.
tags: [echo-veil, memory, security, encryption, readiness]
status: active
updated: 2026-07-24
---

# Echo Veil Security Status

Echo Veil is a security subsystem when
`echo_veil_protection=required`, not a feature flag that may silently fall back.
The only Algo integration owner is `algo_cli.ada_memory_echo_veil`. It delegates
storage, protected indexing, lifecycle, recovery, corruption quarantine, and
key rotation to `echo_veil.agent_memory.AgentMemory`.

The earlier duplicate Oracle wrapper and plaintext `echo_veil_state.json`
shadow are removed. No `algo_cli/memory/echo_veil_layer.py` implementation is
authoritative. Internal callers use `ada_memory_echo_veil.py`; the package-level
`memory_echo_veil` attribute is a deprecated alias to that same module, not a
second implementation. Adding another adapter is a security regression.

## Runtime identity and policy

Algo accepts Echo Veil only when both the imported source version and installed
distribution metadata match and are in `>=0.5.0,<0.6.0`. Editable installs and
unpinned direct-URL or local-directory installs are rejected even when their
version strings match. Registry/wheel installs, content-hashed archives, and
VCS installs pinned to a full commit ID are the accepted identity classes.

The persisted Algo configuration contains profile, scope, state-directory, and
policy settings only. It does not contain Echo encryption keys. Echo's
scoped-v2 key manifest contains owner-only key references, while raw key
material remains in permission-restricted files owned by Echo Veil.

Policy behavior is explicit:

- `optional`: use Echo when the supported adapter is healthy; otherwise emit a
  warning and retain the existing plaintext memory behavior.
- `required`: block ordinary memory writes and protected recall when Echo
  cannot initialize. Never retry the write into `memory.json`,
  `system_memory.json`, a curated catalog, or another plaintext store.

The readiness report keeps these facts separate:

- `installed`
- `version_supported`
- `enabled`
- `crypto_initialized`
- `write_wired`
- `index_wired`
- `retrieval_wired`
- `persistence_wired`
- `restart_restored`
- `rotation_ready`
- `healthy`

It also reports a non-secret key ID, security schema, quarantine count, rotation
state, degradation state, and an installation-identity class. It does not emit
filesystem paths, raw scope values, keys, payloads, search terms, vectors, or
full exception text. `local_protection_ready` is distinct from
`production_ready`; the latter remains false until the complete release gate
and independent review are recorded.

## Entry-point matrix

| Entry point | Required mode | Optional mode |
|---|---|---|
| `/remember` | Scoped-v2 encrypted Echo write only | Echo when healthy, otherwise warned legacy plaintext |
| Direct `Config.remember_fact` and runtime-agent remember | Same protected bridge as `/remember` | Active backend is explicit |
| Bounded automatic memory capture | Same protected bridge; failure blocks capture | Active backend is explicit |
| Context recall | Echo only; no plaintext-memory fallback | Echo first, with identified legacy fallback |
| `/forget` | Echo deletion and authenticated tombstone | Active backend's deletion semantics |
| Curated/history promotion, demotion, archive, and catalog reindex | Prohibited; these stores are outside Echo | Deliberately plaintext under the existing policy |
| Harness refresh/embedding, wiki, graph, lessons, transcripts, session history | Deliberately outside Echo; no protection claim | Deliberately outside Echo; no protection claim |
| Legacy import or migration | Explicit operator workflow only | Explicit operator workflow only |
| `ollama-cli` compatibility command | Same Algo runtime and authoritative bridge | Same Algo runtime and active policy |

In-memory copies used to assemble an authorized prompt remain plaintext in the
Algo process. Local Ollama receives plaintext for embedding. Logs, model
providers, prompts, crash reports, backups, swap, host compromise, and stores
outside the matrix are not protected by Echo.

## Evidence and remaining blockers

Repository tests now exercise required-mode write refusal, version drift,
ordinary runtime writes, absence of legacy shadow files, protected disk state,
fresh-process restart recall, scope rejection, deduplication, reconciliation,
corruption quarantine, lost-key behavior, and resumable rotation.

This source tree is not yet a reproducible production installation. The
hardened Echo implementation must first be released as a lockable artifact,
added to Algo's locked optional dependency set, and verified from a clean
installed wheel. The current managed runtime's mismatched source/distribution
versions are therefore correctly blocked. Backup/restore qualification,
process-termination fault injection during commit/rotation, measured overhead
in the installed Algo process, and independent security review also remain
promotion blockers.

Do not set `production_ready=true` until CI evidence satisfies the hard gate in
Echo Veil's `docs/LOCAL_AGENT_SECURITY.md`, including the actual installed Algo
runtime. Green unit tests or `healthy=true` alone are insufficient.
