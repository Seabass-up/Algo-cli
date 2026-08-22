# Nathan prompt-capsule contract

## Status

Prompt capsules are a hardening candidate with three reversible modes:

- `legacy` sends the prior monolithic system prompt;
- `shadow` sends the exact legacy prompt while constructing and measuring the
  capsule candidate without changing the model request;
- `capsule` sends the candidate only when every required section fits its
  declared budget. Any required overflow or protected-memory conflict falls
  back to the complete legacy prompt.

The repository default remains `shadow` until the frozen live legacy-versus-
capsule cell passes. Deterministic local qualification is necessary but is not
sufficient to make capsules the default or support a public speed claim.

## Authority boundaries

The immutable core always precedes optional context and retains:

- active model and provider truth;
- runtime policy, capability ceilings, approval requirements, and
  postcondition checks;
- the selected mutable-memory authority and Echo Veil exclusivity;
- privacy and credential-handling constraints;
- workspace containment and evidence-based completion rules;
- the current session mode and one-shot or interactive stop contract.

Prompt text never grants tool authority. Capsule-related tools are only hints
to the existing exact-schema selector. The central action registry, runtime
policy, execution guardrails, and scoped grants remain authoritative.

Conversation summaries, memory retrieval, unresolved attempts, harness RAG,
and graph retrieval are separate evidence classes. Summaries are explicitly
lossy. Retrieval cannot silently overwrite live state, and competing memory
candidates remain distinguishable. Dynamic context is appended after the
stable core and activated capsules to preserve the largest reusable prefix.

## Registry contract

`algo_cli/nathan_prompt_capsules.py` is the sole capsule registry. Every entry
declares a stable identifier and version, supported phases, priority, maximum
tokens, trust class, ambiguity behavior, dependencies, conflicts, related
tools, related commands, deterministic activation, and rendering behavior.

Registry validation rejects duplicate identities, malformed schemas, unknown
dependencies, dependency cycles, asymmetric conflicts, and unsafe identifiers.
Required policy or runtime-guidance capsules fall back to legacy on overflow;
optional guidance is omitted with a content-free reason receipt. Capsule text
is never cut mid-contract.

Single generic nouns are not sufficient for broad activation. For example,
incidental `agent-harness benchmark` wording does not activate Agent runtime or
harness-search guidance. Agent and harness capsules require an explicit phase,
command, compound intent, named store, or relevant tool binding.

## Budgets and receipts

The candidate enforces independent budgets for:

- immutable core and activated static capsules;
- lossy conversation continuity;
- mutable-memory evidence;
- unresolved execution attempts;
- total system-prompt context.

`/context explain` exposes only structural decisions: configured and sent
mode, capsule IDs, omission/fallback reasons, registry digest, and tool
selection reasons. It does not expose prompt text, user text, memory content,
or reversible hashes of short user input. Optional telemetry records bounded
counts and timings only and cannot fail prompt construction.

Operators can use `/context capsules legacy`, `/context capsules shadow`, or
`/context capsules on`. One-shot processes can use
`--prompt-capsules legacy|shadow|capsule` without mutating saved configuration.

## One-shot workspace authority

`--approval-mode workspace` is an explicit process-local authority for
headless work inside `--cwd`. It can confirm only the four curated
workspace-targeted actions: `write_file`, `edit_file`, `batch_edit`, and
`run_shell`. Existing safe-mode, path-containment, read-before-edit, command,
lease, outcome, and verification controls still apply. Memory, network,
credential, plugin, external, destructive, and handoff actions do not inherit
this authority. `never` and `auto` retain their previous semantics, and the
workspace mode is removed before configuration is saved.

Baseline read grants allow multiple atomic uses because every read is already
independently admitted by the same read-only workspace ceiling. This prevents
parallel reads from racing over a one-use grant without expanding the read
boundary.

Closed adapter preconditions that prove no mutation occurred, such as an edit
whose old string was absent or a non-overwrite write to an existing file, are
reported as retryable failures. Shell failures, interruptions, timeouts,
partial batches, external effects, and ambiguous adapter errors remain unknown
outcomes and cannot be retried automatically.

## Qualification and claim boundary

The source-bound deterministic ablation compares exact legacy and capsule
construction over ordinary, named-file, code, PDF, Grok, memory-conflict,
slash-command, Agent, and one-shot cases. It checks the untouched legacy
baseline, deterministic repeatability, activation, exact schema accounting,
fallback behavior, prompt reduction, and bounded local construction latency.

The competitor runner can interleave `algo_cli_legacy` and
`algo_cli_capsule` variants with the same model, task digest, machine, timeout,
workspace authority, order policy, and independent checkers. A live cell is
invalid if any checker, process, scope, protected-input, or structured-output
gate fails.

Current live attempts exposed and helped repair an unusable one-shot mutation
boundary, a parallel-read grant race, two activation false positives, and
unknown-outcome overclassification. The latest frozen live cell is not a pass:
a local model run stalled after its first response round until timeout. The
default therefore remains `shadow`, public claims remain ineligible, and the
active hardening freeze continues to block tagging, release, and website
publication.
