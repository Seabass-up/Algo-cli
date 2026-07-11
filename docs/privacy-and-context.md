---
title: Privacy and local context
description: How Algo CLI discovers, stores, and sends local context.
status: active
tags: [privacy, harness, cloud, context, consent]
---

# Privacy and local context

Algo CLI starts with repository-provided documentation, skills, and files that the user creates under `~/.algo_cli`. It does not index other agent stores by default.

## Always-sent model context

Every normal chat request sends the active conversation and the assembled Algo CLI system context to the selected inference provider. The assembled context includes `SOUL.md`, `IDENTITY.md`, `USER.md`, saved memories, and relevant retrieved lessons when available. If the active model uses a cloud provider, this content may leave the machine.

Automatic memory capture is enabled by default. Its bounded completion gate may save an explicit, high-confidence durable statement after a successful turn. `/memory-auto off` stops future automatic captures; it does not remove existing entries. Inspect existing entries with `/memories` and remove one with `/forget ID`.

## Optional sources

- `/harness external on` enables discovery of supported local agent stores such as Codex, Claude, OpenClaw, Mercury, and shared agent skills.
- `/icl on` enables index-compute-lab retrieval and automatic graph context.
- `/code-rag on` enables working-directory source indexing and relevant-snippet retrieval.
- `/skills on` enables bounded run-summary capture and automatic local skill-candidate crystallization.
- Entries in `~/.algo_cli/harness_roots.json` are treated as explicit user-provided roots.

Disable an optional source with `/harness external off`, `/icl off`, or `/code-rag off`. Changing either harness setting rebuilds the generated harness index so disabled records are removed. `/code-rag off` also purges every persisted code-index file.

## Working-directory code retrieval boundary

- **Command boundary:** `/code-rag status` is read-only. `/code-rag on` records the current explicit consent version; a legacy saved `code_rag_enabled: true` value does not count as consent. Model-invoked `on` and `off` commands require approval.
- **Index boundary:** when enabled, Algo CLI scans supported source files beneath the active cwd, skips hidden/build/vendor directories and secret-like filenames, rejects symlinks that escape the cwd, and stores chunks plus local embeddings in `~/.algo_cli/code_index/`. `/code-rag off` deletes those generated index files, not the source files.
- **Provider boundary:** embeddings are generated through local Ollama. Retrieved source snippets are then added to the active chat request, so they may leave the machine when the selected inference provider is remote.

## Skill run history

Skill run-history capture and automatic crystallization are off by default. `/skills on` opts in to bounded completed-run summaries at `~/.algo_cli/private/run_history.jsonl`. After the configured interval, a genuinely local, non-embedding Ollama model may turn qualifying patterns into quarantined skill candidates for explicit review. Crystallization skips when no such local model is available and never falls back to a cloud provider. `/skills off` stops future capture and automatic crystallization but does not delete prior history. Older installs may also have a legacy read-only input at `~/.algo_cli/run_history.jsonl`.

## Provider boundary

Retrieved context becomes part of the model request. If the selected model uses a cloud provider, enabled local context may leave the machine. Enable external sources only when their content is appropriate for the selected provider.

Algo CLI removes common credential forms and indexes connector/MCP JSON as metadata only. Redaction is a defense-in-depth measure, not a guarantee that arbitrary sensitive prose will be detected.

## Local state

Runtime configuration, generated indexes, memories, identity files, and credentials live under `~/.algo_cli` unless `ALGO_CLI_CONFIG_DIR` overrides the location. These files are not part of the source distribution. Keep the directory private and exclude it from repositories and backups that are shared publicly.

Automatic memory capture is bounded and privacy-gated. Inspect or disable it with `/memory-auto status` and `/memory-auto off`.
