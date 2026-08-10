---
title: Privacy and local context
description: How Algo CLI discovers, stores, and sends local context.
status: active
updated: 2026-08-10
last_reviewed: 2026-08-10
runtime_version: "Algo CLI v0.18.0"
verification_revision: "215d7cb044fd530809af7c9d0a375e1d3bb792d5"
tags: [privacy, harness, cloud, context, consent]
---

# Privacy and local context

Algo CLI starts with repository-provided documentation, skills, and files that the user creates under `~/.algo_cli`. It does not index other agent stores by default.

> **Runtime check:** Privacy defaults and enabled context sources are runtime state. Run `/harness status`, `/memory-auto status`, `/icl`, and `/code-rag status` in the active Algo CLI process before describing them as enabled. A historical 2026-07-24 non-Echo review found external agent stores disabled with zero indexed external records and index-compute-lab auto-inject enabled; Echo-selected runs now override that legacy flag and refuse graph access.

## Always-sent model context

Every normal chat request sends the active conversation and the assembled Algo CLI system context to the selected inference provider. With Echo Veil disabled, that context can include local `SOUL.md`, `IDENTITY.md`, `USER.md`, saved memories, and relevant retrieved lessons. While Echo Veil owns memory, those mutable plaintext identity/continuity files are not read, stat-keyed, scaffolded, or injected; only repo-shipped product identity text and governed Echo context are assembled. If the active model uses a cloud provider, the assembled context may leave the machine.

Automatic memory capture is off by default. `/memory-auto on` records explicit consent for the current capture policy; legacy or unknown saved booleans fail closed. Once enabled, the bounded completion gate may save an explicit, high-confidence durable statement after a successful turn. `/memory-auto off` stops future automatic captures and clears that consent; it does not remove existing entries. Inspect existing entries with `/memories` and remove one with `/forget ID`.

## Optional sources

- `/harness external on` enables discovery of supported local agent stores such as Codex, Claude, OpenClaw, Mercury, and shared agent skills.
- `/icl on` enables index-compute-lab retrieval and automatic graph context.
- `/code-rag on` enables working-directory source indexing and relevant-snippet retrieval.
- `/skills on` enables bounded run-history capture only when Echo is disabled. Under Echo authority, accepted run events are projected to keyed, content-free receipts; plaintext previews and automatic crystallization remain disabled.
- Entries in `~/.algo_cli/harness_roots.json` are treated as explicit user-provided roots.

Disable an optional source with `/harness external off`, `/icl off`, or `/code-rag off`. Changing either harness setting rebuilds the generated harness index so disabled records are removed. `/code-rag off` also purges every persisted code-index file.

## Working-directory code retrieval boundary

- **Command boundary:** `/code-rag status` is read-only. `/code-rag on` records the current explicit consent version; a legacy saved `code_rag_enabled: true` value does not count as consent. Model-invoked `on` and `off` commands require approval.
- **Index boundary:** when enabled outside Echo authority, Algo CLI scans supported source files beneath the active cwd, skips hidden/build/vendor directories and secret-like filenames, rejects symlinks, hardlinks, special files, and sources that change during descriptor-bound reads, and stores chunks plus local embeddings in `~/.algo_cli/code_index/`. Echo preflight purges this plaintext-derived index before protected work. `/code-rag off` also deletes generated index files, not source files. An ordinary copied file in the operator-selected workspace is workspace input; Algo cannot infer that its bytes were copied from a legacy memory file.
- **Provider boundary:** embeddings are generated through local Ollama. Retrieved source snippets are then added to the active chat request, so they may leave the machine when the selected inference provider is remote.

## Skill run history

Skill run-history capture and automatic crystallization are off by default. When Echo is disabled, `/skills on` opts in to bounded completed-run summaries at `~/.algo_cli/private/run_history.jsonl`; a genuinely local, non-embedding Ollama model may later turn qualifying patterns into quarantined candidates for explicit review. Crystallization never falls back to a cloud provider.

When Echo is selected, the same surface changes contract: the protected run-history store contains only domain-separated keyed receipts and structural counters, is bound to a persistent key and external monotonic head, and is validated during the Echo auxiliary preflight. It never stores task, argument, result, or model-output prefixes. Crystallization is disabled because content-free receipts are not semantic training material. Unproven legacy skill files are quarantined outside the active harness root, and cached mutable skill records are removed before retrieval. `/skills off` stops future capture; it does not silently reinterpret protected receipts as plaintext history.

## Echo-exclusive local path boundary

When Echo is selected, model-callable `run_shell` is disabled because a free-form shell cannot prove that it avoided legacy memory. Typed read, list, search, edit, write, PDF, vision, Git, and session-path actions reject `~/.algo_cli`, `~/.ollama_cli`, legacy backups and migration residues, index-compute-lab, external agent stores, configured extra roots, and pre-existing symlink or hardlink aliases. Repository-shipped Algo contracts remain available as immutable product evidence. Legacy graph queries/reindexing, graph-note plaintext writes, Intuition, lessons indexes, x-search cache writes, and mutable external harness roots are likewise disabled or purged before a protected model turn.

This path gate assumes the local operating-system account is trusted. Validation and the underlying tool open are separate operations; a malicious same-UID process that swaps filesystem objects between them is outside this runtime layer's threat boundary. Algo does not call this gate TOCTOU-proof. Operator migration or forensic work requires a separate bounded, approval-gated workflow rather than a model-set flag or free-form shell escape.

The no-follow rule is intentionally lexical and conservative. On macOS, standard aliases such as `/var -> /private/var` and `/tmp -> /private/tmp` are refused even for otherwise ordinary files; use the canonical `/private/var/...` or `/private/tmp/...` spelling. Pre-existing aliases are rejected rather than resolved because resolving an operator-defined alias could cross a protected-memory boundary.

## Provider boundary

Retrieved context becomes part of the model request. If the selected model uses a cloud provider, enabled local context may leave the machine. Enable external sources only when their content is appropriate for the selected provider.

Algo CLI removes common credential forms and indexes connector/MCP JSON as metadata only. Redaction is a defense-in-depth measure, not a guarantee that arbitrary sensitive prose will be detected.

## Local state

Runtime configuration, generated indexes, memories, identity files, and credentials live under `~/.algo_cli` unless `ALGO_CLI_CONFIG_DIR` overrides the location. These files are not part of the source distribution. Keep the directory private and exclude it from repositories and backups that are shared publicly. Echo protects its own selected memory authority and authenticated auxiliary receipts; it does not encrypt arbitrary natural-language prompts, current-turn model context, provider requests, operator-created transcripts, crash dumps, swap, or unrelated files.

Automatic memory capture is opt-in, bounded, and privacy-gated. Inspect or change it with `/memory-auto status|on|off`.
