# T3 Code parity review — 2026-07

This review compares Algo CLI with T3 Code at immutable revision
[`c1ec1915`](https://github.com/pingdotgg/t3code/tree/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526).
It is a product-capability comparison, not a model-quality benchmark. T3 Code is primarily a
desktop/web/mobile control plane over other coding agents; Algo CLI is a terminal-native agent
runtime with its own retrieval, memory, reasoning, verification, and multi-agent systems.

## Evidence-based scorecard

| Capability | T3 Code evidence | Algo CLI status after this pass | Verdict |
|---|---|---|---|
| Durable threads | Provider sessions persist cwd, model, mode, and resume identity in [`ProviderService.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/provider/Layers/ProviderService.ts) | Parent/child records persist workspace identity, branch, initial/current HEAD, bounded turns, block evidence, and full working-state digests; resume validates and restores the exact state | Core parity; T3 still has richer live provider cursors and interruption |
| Worktree lifecycle | Thread/worktree creation is coordinated in [`ws.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/ws.ts) | `/worktree` and default-isolated `/agent fork` use structured Git calls, repository-identity hashes, deterministic collision probing, exact-state checkpoints with immutable child bases, active-record preservation, and tracked/untracked/ignored cleanup gates | Algo advantage on collision and cleanup safety |
| Commit → push → PR | Stacked Git actions live in [`GitManager.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/git/GitManager.ts) | `/ship` provides scoped commit, state fingerprints, bounded per-commit metadata/path/delta scrubs, fresh remote divergence checks, immutable destination-bound refspecs, resumable phases, and draft-by-default GitHub PR creation | Core GitHub flow parity; T3 supports more source-control hosts and richer generated copy |
| Provider breadth | Five drivers—Codex, Claude, Cursor, Grok, OpenCode—are registered in [`builtInDrivers.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/provider/builtInDrivers.ts) | Native routes cover local Ollama, Ollama Cloud, xAI, and ChatGPT/Codex; direct selection and dashboard state are now consistent | Gap: no isolated multi-instance provider SPI; no Claude/OpenCode/Cursor runtime adapters |
| Multi-agent reasoning | Independent threads and provider-native subagents | `/agent team` has bounded 2–4 role fan-out, deterministic joins, one write owner, route budgets, policy chains, Git attribution, recovery, and verification | Algo advantage |
| Retrieval and memory | Project/session persistence; no equivalent first-class hybrid harness RAG | BM25/vector RRF, context packing, source provenance, curated harness knowledge, privacy-gated memory admission, and local intuition recall | Algo advantage |
| Checkpoints and rollback | Hidden refs, per-turn/thread diffs, filesystem restore, and provider rollback in [`CheckpointReactor.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/orchestration/Layers/CheckpointReactor.ts) | Git evidence and isolated branches exist, but no user-facing turn checkpoint/restore command | Gap |
| Integrated terminal | Persistent multiplexed PTYs and bounded lifecycle logic in [`terminal/Manager.ts`](https://github.com/pingdotgg/t3code/blob/c1ec1915fc16f3dc1ec5d47d9a97f6210a574526/apps/server/src/terminal/Manager.ts) | Blocking bounded shell tool plus the user's native terminal; no persistent multiplexed PTY | Gap |
| GUI and preview | React desktop/web surfaces, diff review, file browser, preview, command palette | Polished streaming terminal interface and static runtime overview | Different product surface; not parity |
| Secure remote continuity | Authenticated remote environments, reconnect supervision, SSH, and tunnel paths | NDJSON one-shot bridge and loopback harness gateway; no authenticated durable remote session service | Gap |

## Improvements deliberately stronger than the reference

- **Collision resistance:** managed paths include repository identity plus a collision-probed slug;
  same-named repositories and slash-normalized branches cannot silently alias.
- **Data-preserving removal:** cleanup refuses ignored files, which ordinary Git cleanliness checks
  omit and which `git worktree remove` can otherwise delete.
- **Review binding:** `/ship status` fingerprints HEAD, branch, staged/unstaged/untracked state,
  upstream, remote URL, and cached remote refs; `--expect` fails when reviewed state changes.
- **Outgoing evidence:** pushes refresh the remote, reject behind/diverged or mismatched-upstream
  state, and scrub every immutable outgoing commit's raw metadata, literal paths, and delta within
  a cumulative bound. Git object overlays and unresolved LFS payloads are rejected; the reviewed
  object ID is pushed through an isolated, revalidated destination binding. Repository-provided
  scripts are never executed implicitly; Algo's broader public-release scanner remains a separate
  CI gate.
- **Bounded multi-agent ownership:** specialists remain read-only; only the integration pipeline may
  mutate, avoiding the shared-writer conflicts that a multi-thread UI alone does not solve.

## Remaining release gates before claiming full T3 product parity

1. Provider-instance SPI and production adapters for Claude, OpenCode, and Cursor.
2. Persistent cancellable PTY sessions with byte-bounded replay and backpressure.
3. Turn checkpoints, reviewable per-turn diffs, and safe filesystem/provider rollback.
4. Authenticated durable remote sessions with reconnect/resume semantics.
5. A navigable TUI or optional web surface for live threads, approvals, diffs, and terminals.
6. GitLab, Bitbucket, and Azure DevOps publish adapters if cross-host parity is a release goal.

Marketing must say **Algo CLI exceeds T3 in algorithmic multi-agent execution, retrieval/memory,
worktree collision safety, and guarded Git publishing**. It must not say full product parity until
the remaining gates above have executable tests and release evidence.
