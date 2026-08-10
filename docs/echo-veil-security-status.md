---
title: Echo Veil Security Status
description: Authoritative adapter, entry-point classification, and promotion gate for protected Algo CLI memory.
tags: [echo-veil, memory, security, encryption, readiness]
status: active
updated: 2026-08-10
last_reviewed: 2026-08-10
runtime_version: "Algo CLI v0.18.0"
verification_revision: "215d7cb044fd530809af7c9d0a375e1d3bb792d5"
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

Optional mode accepts Echo Veil only when the imported source version and
installed distribution metadata match in `>=0.6.0,<0.8.0`; editable and
unpinned direct-URL or local-directory installs are rejected. Required mode is
stricter: PEP 610 metadata must bind version `0.7.0`, the canonical upstream
repository, requested revision, and resolved commit to
`e450260505f0fa5ad9f17bc9e28eac6db3f46e22`. A same-version registry wheel,
archive, different repository, or different commit does not satisfy required
protection.

The development/runtime candidate is pinned to Echo Veil 0.7.0 commit
`e450260505f0fa5ad9f17bc9e28eac6db3f46e22` through the
`algo-cli-runtime[echo-veil]` extra. This source pin is qualification evidence,
not a public Echo Veil release or a production-readiness claim.

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

- `optional`: when Echo is disabled, use the legacy memory backend. When Echo
  is enabled, it is the sole ordinary-memory read/write/delete authority;
  initialization or recall failure omits/refuses that memory operation without
  consulting or updating a legacy plaintext shadow.
- `required`: block ordinary memory writes and protected recall when Echo is
  disabled or cannot initialize. Ordinary chat and Agent Block runs both stop
  before their first model call when required protected recall cannot be
  established. Never retry the write or recall through `memory.json`,
  `system_memory.json`, a curated catalog, or another plaintext store.

Explicit behavioral lessons follow the same authority split. `/lesson` and the
`append_lesson` tool create Echo Short-Term records with caller/source
provenance whenever Echo is selected. They never append a plaintext lesson
shadow. Legacy `lessons-learned.md` content and its embedding index are omitted
from prompt construction, automatic reindexing, and `/lessons reindex` until
Echo is disabled.

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
- `qualified_runtime_identity`
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
- `live_probe_performed`

Static readiness is the default and never constructs Echo. A caller must
explicitly request a live probe before `healthy` can become true because Echo
construction and inventory can recover incomplete operations, migrate
contracts, prune expiry, or update lifecycle accounting. Live doctor and list
surfaces are consequently mutation-classified and approval-gated. The report
also includes a non-secret key ID, security schema, quarantine count, rotation
state, degradation state, and an installation-identity class. It does not emit
filesystem paths, raw scope values, keys, payloads, search terms, vectors, or
full exception text. `local_protection_ready` is distinct from
`production_ready`; the latter remains false until the complete release gate
and independent review are recorded.

## Entry-point matrix

| Entry point | Required mode | Optional mode |
|---|---|---|
| `/remember` | Scoped-v2 encrypted Echo write only | Echo-only when enabled; legacy-only when disabled; never a fallback/shadow |
| `/lesson` and model tool `append_lesson` | Echo Short-Term write with explicit lesson provenance; no plaintext lesson/index write | Echo-only when enabled; `lessons-learned.md` only when disabled |
| Model tools `echo_veil_remember`, `echo_veil_refresh_live`, and `echo_veil_promote` | Same protected lifecycle contract; every mutation is runtime-approval gated | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_recall` and `echo_veil_context` | Session-preapproved ranked recall or authenticated logic trace with explicit lifecycle metadata | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_list` and `echo_veil_doctor` | Approval-gated local lifecycle operations; construction may recover, migrate, prune, or account usage | Refused; these names never fall back to legacy memory |
| Model tools `echo_veil_forget` and `echo_veil_reindex` | Protected destructive maintenance with explicit runtime approval | Refused; these names never fall back to legacy memory |
| Direct `Config.remember_fact` and runtime-agent remember | Same protected bridge as `/remember` | Same selected-backend rule; enabled Echo never shadows to plaintext |
| Explicitly opted-in bounded automatic memory capture | Same protected bridge; stale or missing consent and protection failure both block capture | Same selected-backend rule; agent tool receipts suppress duplicate capture |
| Ordinary-chat context and system contract | Doctor-backed Echo recall plus the four-layer ritual; a fault stops before the first model call | Echo-only context when enabled, with no legacy fallback; legacy context only when disabled |
| Agent-pipeline context recall | Echo only; a protected-recall fault fails the run before its first model block, with no plaintext-memory fallback | Echo-only when enabled; an outage omits optional memory rather than consulting legacy state |
| `/forget` | Echo deletion and authenticated tombstone | Echo deletion when enabled; legacy deletion only when disabled |
| Curated/history promotion, demotion, archive, and catalog reindex | Prohibited while Echo is authoritative | Prohibited while Echo is enabled; available only when Echo is disabled |
| Harness refresh and mutable external context | Cached mutable-memory, personal-continuity, user-skill, graph, x-search, and arbitrary extra-root records are purged and cannot be re-indexed; only closed repo-shipped product evidence remains | Same exclusion whenever Echo is selected; legacy sources return only after Echo is disabled |
| Prompt, conversation, summary, and tool persistence | Echo tool calls/results and protected slash aliases are projected before persistence; summaries with unrecoverable provenance are dropped; natural-language user/assistant text remains an operator/provider boundary | Same selected-authority projection |
| Local identity continuity (`SOUL.md`, `IDENTITY.md`, `USER.md`) | Not read, stat-keyed, scaffolded, or injected; prompts use only immutable repo-shipped product identity text, and profile mutation/model `/identity` access is refused | Same whenever Echo is selected; local identity files are available only when Echo is disabled |
| Goal, Agent thread/journal, candidate, and skill run state | Structural metadata plus domain-separated persistent HMAC receipts and external monotonic heads; protected bodies are never persisted | Same whenever Echo is selected |
| Intuition, knowledge graph, lessons index, x-search cache, and model filesystem tools | Plaintext authorities are disabled or purged; free-form shell is unavailable and typed paths reject legacy-memory roots/aliases | Same whenever Echo is selected |
| Legacy import or migration | Explicit operator workflow only | Explicit operator workflow only |
| `ollama-cli` compatibility command | Same Algo runtime and authoritative bridge | Same Algo runtime and active policy |

In-memory copies used to assemble an authorized prompt remain plaintext in the
Algo process. Local Ollama receives plaintext for embedding. Model providers,
natural-language prompts and assistant restatements, explicitly saved operator
transcripts, crash reports, backups, swap, and host compromise are not encrypted
by Echo. Model-callable filesystem validation assumes a trusted local OS account;
a malicious same-UID process racing path validation and tool open is outside this
layer and is not described as TOCTOU-proof.

## Evidence and remaining blockers

Repository tests now exercise required-mode write refusal, version drift,
ordinary runtime writes, absence of legacy shadow files, protected disk state,
fresh-process restart recall, scope rejection, deduplication, reconciliation,
corruption quarantine, lost-key behavior, resumable rotation, four-layer prompt
injection, and zero-model-call failure when required prompt context is
unavailable. They also exercise optional-enabled Echo-only routing, direct
legacy-deletion refusal, lifecycle-read approval, static nonmutating readiness,
agent explicit-write suppression, and content-free attempt receipts.

### Reproducible verification stamp

The source contract was last verified on 2026-08-09 from the hardening
candidate based on Algo CLI revision
`215d7cb044fd530809af7c9d0a375e1d3bb792d5`. The generated receipt at
`hardening/grace-m8-local-qualification.json` records its exact source digest
outside this source-bound document and binds fixture digest
`sha256:5d94b7afade9ce7aab941948a6120fb8090a3c2d9bf6e600f2d92b9d9372b659`.
It records 9 local passes, 5 external production-browser blocks, 0 failures,
and `public_claim_eligible=false`. The protected-memory source matrix uses:

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_ada_memory_echo_veil.py \
  tests/test_julia_memory_runtime.py \
  tests/test_tool_context.py \
  tests/test_harness.py::test_harness_stats_reports_truthful_echo_veil_readiness
```

Required result: every collected test passes and the command exits zero. The
exact count intentionally is not a release invariant because adversarial
coverage grows over time. `PYTHONPATH=.` forces the tests to use the current
checkout instead of a stale installed package. The source pin is
mechanically visible in both dependency files with:

```bash
rg -n "e450260505f0fa5ad9f17bc9e28eac6db3f46e22" pyproject.toml uv.lock
.venv/bin/python scripts/henry_echo_veil_dependency_audit.py
```

The normal user entry point is a separate gate. On 2026-08-09 it resolved Echo
Veil `0.7.0` from a local archive and reported the expected shared Qwen3
profile, protected layers, `all_records_shielded=true`, healthy local staging,
and `production_ready=false`. However, its archive origin was not the committed
VCS lock and its installed Algo adapter differed from this checkout. That
runtime is useful local-staging evidence, but it is not reproducible parity and
does not satisfy the installed-release gate. The project virtual environment
now resolves Echo `0.7.0` from the exact locked commit and passes a PEP 610 plus
wheel-RECORD content audit; an isolated clean Algo wheel remains required.

The runtime tool registry separately passed its curated-authority audit with
the nine `echo_veil_*` tools. Recall and context are classified as
session-preapproved local lifecycle mutations. Inventory and doctor are also
local lifecycle mutations and require action-time approval; remember, refresh,
promote, forget, and reindex remain approval-gated. Durable attempt entries
retain only status, byte/character counts, and a keyed digest, never a raw tool
or Echo payload prefix. A long-running Algo process must be restarted before it
can load newly changed Python modules.

This source tree is not yet a reproducible production installation. The
hardened Echo implementation is locked for development through a full VCS
commit and must still be released as a signed/versioned artifact and verified
from a clean installed wheel. The normal user entry point must also be rebuilt
from that committed source rather than retained as archive-built staging.
Backup/restore qualification,
process-termination fault injection during commit/rotation, measured overhead
in the installed Algo process, and independent security review also remain
promotion blockers.

Do not set `production_ready=true` until CI evidence satisfies the hard gate in
Echo Veil's `docs/LOCAL_AGENT_SECURITY.md`, including the actual installed Algo
runtime. Green unit tests or `healthy=true` alone are insufficient.
