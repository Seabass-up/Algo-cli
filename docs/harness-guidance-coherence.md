# Harness Guidance Coherence

Local repair scope: the source imported by the existing launcher, not a claim
that a public release or already-running process has changed.

## Operational Skills

The bundled memory and recovery skills declare `runtime_contract` versions
`continuum-memory-v1` and `mutation-recovery-v1`. Indexing rejects their legacy
copies without the matching contract. Cached entries and direct harness reads
are checked too, including a source changed after indexing. An unchanged-file
signature cannot preserve a pre-contract cache entry. This is compatibility
checking for two known skills, not semantic validation of arbitrary third-party
instructions and not an authorization boundary. Historical reference documents
are not suppressed merely for mentioning retired terms.

Continuum is the sole mutable memory authority. Reference refresh is lexical;
it does not reload Python code, refresh memory or select an embedding service.
Unknown mutation outcomes require read-only reconciliation before replay.
Read-before-write failures require reading, not changing tools to avoid the check.

## Runtime Guidance

Prompt assembly, action discovery and indexed catalog metadata use the same
configured owner catalog guidance. The live target is `<config-dir>/ALGO.md`,
including when it does not exist yet. `docs/ALGO.md` remains an empty public
starter. No user catalog or custom kernel is copied into the public starter.
Automatic lexical reference retrieval reports `harness-lexical-index`, derived
from its query snapshot's backend metadata, rather than a hybrid-index label.

See [Continuum Memory](continuum-memory.md) for explicit project selection and
the difference between narrowed, global and unknown-project context. This repair
does not demote global required records or rewrite remembered historical counts.

## Decision Diagnostics

The model receives an operational checkpoint: status, refusal category, actual
typed specialist answers with probabilities, selected references, rejection
checks, locality and a diagnostic lookup. No routing distribution or full
reference catalog is injected by default. Deferred routing explicitly says no
specialist was dispatched; it does not become a provider failure or a successful
answer to the original question. Decisions never grant execution authority.

`decision_status(checkpoint=N)` returns that task's full diagnostic receipt
without probing providers or running inference. Complete detail is bounded to
64 KiB per receipt and the last three checkpoints, retained in process memory
only. The lookup reports unavailable after a new task, process restart or
oversized detail, rather than returning another task's record. Content-free
receipt hashes remain in runtime status; no new plaintext memory store is added.
The attempt ledger intentionally does not expose target paths. Diagnose unknown
effects from authorized target observations and original action evidence, not
from guesses about a digest.

## Verification

Regressions: `tests/test_guidance_coherence.py`,
`tests/test_decision_checkpoints.py`, plus the existing harness, prompt, memory,
tools, main-loop and configuration tests. Jev assisted with bounded contract
review; its judgments were not used as permission or proof that checks passed.
Restart Algo CLI to load Python repairs. The next refresh can rebuild reference
records; `/harness refresh` alone cannot activate runtime code changes.
