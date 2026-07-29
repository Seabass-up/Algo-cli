---
title: Echo Veil Security Status
description: Authoritative adapter, entry-point classification, and promotion gate for protected Algo CLI memory.
tags: [echo-veil, memory, security, encryption, readiness]
status: active
updated: 2026-07-25
last_reviewed: 2026-07-25
runtime_version: "Algo CLI v0.18.0"
verification_base_revision: "215d7cb044fd530809af7c9d0a375e1d3bb792d5"
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

Algo accepts Echo Veil only when the imported source and distribution versions
both equal `0.7.0`, the installation metadata identifies the canonical
`Seabass-up/echo-veil` VCS source, and the resolved full commit equals
`e94be9e649048273ab74eb1150e65ac9481596d9`. Editable, registry-only,
archive-only, unpinned, alternate-repository, and differently pinned builds are
rejected even when their version strings match.

The development/runtime candidate is pinned to Echo Veil commit
`e94be9e649048273ab74eb1150e65ac9481596d9` through the
`algo-cli-runtime[echo-veil]` extra. This source pin is qualification evidence,
not a public Echo Veil release or a production-readiness claim.

`algo-cli echo status` and equivalent natural status wording report installed,
Algo-qualified, and canonical-upstream versions independently. Failure to
reach the canonical upstream is reported as `unknown`; it is never converted
into an “up to date” claim. `algo-cli update echo` and `update echo` build the
qualified revision in a private staging directory, exercise its API,
encryption-at-rest, lifecycle, wrong-scope rejection, doctor, and restart
persistence, then install the exact VCS requirement and repeat verification in
a fresh isolated process. A failed active verification triggers a
best-effort rollback to the previous reviewed revision. Neither command asks a
model to construct shell actions or edits installed package source.

The persisted Algo configuration contains profile, scope, state-directory, and
policy settings only. It does not contain Echo encryption keys. Echo's
scoped-v2 key manifest contains owner-only key references, while raw key
material remains in permission-restricted files owned by Echo Veil.

Algo defaults to the same-user shared authority
`echo-universal-qwen3-v1`, scope `local-user`, and 1,024-dimensional
`qwen3-embedding:latest` vectors. The embedder defaults to a bounded
16,384-token CPU runner with zero post-operation keep-alive, preventing
protected recall from evicting or throttling a large local agent model in
Ollama GPU residency. This resource isolation does not skip the doctor, recall,
answerability gate, or fail-closed outage behavior. Every normal bridge
operation also opens and closes its own bounded Echo adapter. Algo therefore
does not retain a process-lifetime writer lease, while Echo still serializes
each operation against Codex, Claude Code, OpenClaw, and other harnesses using
that profile. A different user, authorization domain, or embedding identity
requires a different profile.

Every in-process bridge write binds `caller:algo-cli` into the protected
provenance contract and keeps the operation source separately as
`algo-cli:<source>`. The caller marker identifies the invoking harness; it does
not qualify as the non-caller evidence required for Long-Term promotion or
Contextual Logic.

Policy behavior is explicit:

- `optional`: use Echo when the supported adapter is healthy; otherwise emit a
  warning and retain the existing plaintext memory behavior.
- `required`: block ordinary memory writes and protected recall when Echo is
  disabled or cannot initialize. Ordinary chat and Agent Block runs both stop
  before their first model call when required protected recall cannot be
  established. Never retry the write or recall through `memory.json`,
  `system_memory.json`, a curated catalog, or another plaintext store.

Required mode injects one Echo operating contract into ordinary-chat and Agent
Block system prompts. The runtime performs doctor-backed protected recall for
each substantive task and admits at most three ranked records into the model
prompt. A strictly closed-form exact response keeps the doctor-backed shield
preflight but omits semantic payloads and tool schemas that cannot affect the
answer. The model must use Contextual Logic for causal or decision questions,
preserve ambiguous and competing records, respect the four-layer promotion
policy, and treat degraded recall as non-semantic,
read-only, and non-authoritative.

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
- `layer_contract_wired`
- `context_trace_wired`
- `competing_memory_wired`
- `content_policy_wired`
- `live_refresh_wired`
- `all_records_shielded`
- `rotation_ready`
- `healthy`
- `host_profile_lease`
- `shared_profile_safe`

It also reports a non-secret key ID, security schema, quarantine count, rotation
state, degradation state, and an installation-identity class. It does not emit
filesystem paths, raw scope values, keys, payloads, search terms, vectors, or
full exception text. `local_protection_ready` is distinct from
`production_ready`; the latter remains false until the complete release gate
and independent review are recorded.

## Entry-point matrix

| Entry point | Required mode | Optional mode |
|---|---|---|
| `echo status`, `echo review`, and `update echo` | Deterministic package status or source-qualified staged update; no model or generic shell plan | Same package-maintenance path |
| `/remember` | Scoped-v2 encrypted Echo write only | Echo when healthy, otherwise warned legacy plaintext |
| Model tools `echo_veil_remember`, `echo_veil_refresh_live`, and `echo_veil_promote` | Same protected lifecycle contract; every mutation is runtime-approval gated | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_recall` and `echo_veil_context` | Session-preapproved ranked recall or authenticated logic trace with explicit lifecycle metadata | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_list` and `echo_veil_doctor` | Bounded lifecycle-neutral inventory or non-secret readiness | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_forget` and `echo_veil_reindex` | Protected destructive maintenance with explicit runtime approval | Refused; these names never fall back to legacy memory |
| Direct `Config.remember_fact` and runtime-agent remember | Same protected bridge as `/remember` | Active backend is explicit |
| Bounded automatic memory capture | Same protected bridge; failure blocks capture | Active backend is explicit |
| Ordinary-chat context and system contract | Doctor-backed Echo recall plus the four-layer ritual; a fault stops before the first model call | Existing optional-memory behavior |
| Agent-pipeline context recall | Echo only; a protected-recall fault fails the run before its first model block, with no plaintext-memory fallback | Echo first, with identified legacy fallback |
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
corruption quarantine, lost-key behavior, resumable rotation, four-layer prompt
injection, and zero-model-call failure when required prompt context is
unavailable.

### Reproducible verification stamp

The source contract was last verified on 2026-07-24 at Algo CLI revision
`215d7cb044fd530809af7c9d0a375e1d3bb792d5` with:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_ada_memory_echo_veil.py \
  tests/test_harness.py::test_harness_stats_reports_truthful_echo_veil_readiness \
  tests/test_julia_curated_memory_contracts.py
```

Result: `50 passed`. `PYTHONPATH=.` is intentional: it forces the tests to use
the current checkout instead of a stale installed package. The source pin is
mechanically visible in both dependency files with:

```bash
rg -n "e94be9e649048273ab74eb1150e65ac9481596d9" pyproject.toml uv.lock
```

The installed runtime is a separate gate. On the same date, the executable at
the normal user entry point resolved Echo Veil `0.6.0` from an archive-pinned
installation, opened `echo-universal-qwen3-v1` with scope `local-user` and
1,024-dimensional Qwen3 embeddings, and reported `security_schema=scoped-v2`,
`all_records_shielded=true`, `healthy=true`,
`local_protection_ready=true`, `host_profile_lease=per-operation`,
`shared_profile_safe=true`, and `production_ready=false`. A disposable
Qwen3 profile exercised active Live, Short-Term, Long-Term, and Contextual
Logic records, protected refresh, semantic top-one recall, authenticated
Contextual Logic evidence, close/reopen restoration, and an Ollama-unavailable
read-only path. The outage returned a keyed result with semantic retrieval
unavailable and refused the write.

The runtime tool registry separately passed its curated-authority audit with
the nine `echo_veil_*` tools. Recall and context are classified honestly as
session-preapproved local lifecycle mutations; inventory and doctor are pure
reads; remember, refresh, promote, forget, and reindex require runtime
approval. A long-running Algo process must be restarted before it can load
newly changed Python modules.

This source tree is not yet a reproducible production installation. The
hardened Echo implementation is locked for development through a full VCS
commit and must still be released as a signed/versioned artifact and verified
from a clean installed wheel. Backup/restore qualification,
process-termination fault injection during commit/rotation, measured overhead
in the installed Algo process, and independent security review also remain
promotion blockers.

Do not set `production_ready=true` until CI evidence satisfies the hard gate in
Echo Veil's `docs/LOCAL_AGENT_SECURITY.md`, including the actual installed Algo
runtime. Green unit tests or `healthy=true` alone are insufficient.
