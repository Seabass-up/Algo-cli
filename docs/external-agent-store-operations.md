---
title: External Agent Store Operations
description: Opt-in discovery, provenance, conflicts, refresh, and recovery for external harness context.
tags: [harness, external-store, operations, privacy, recovery]
status: active
updated: 2026-07-24
last_reviewed: 2026-07-24
runtime_version: "Algo CLI v0.18.0"
verification_revision: "215d7cb044fd530809af7c9d0a375e1d3bb792d5"
---

# External Agent Store Operations

External stores are disabled by default. Enable them only when local Codex, Claude, OpenClaw, `.agents`, Mercury, CLI Agent, or Pi content may safely enter provider prompts.

Do not use the adapter list as an availability claim. Keep these states separate:

| State | Meaning | 2026-07-24 verification |
|---|---|---|
| Supported capability | Algo CLI has discovery adapters for Codex, Claude, OpenClaw, `.agents`, Mercury, CLI Agent, and Pi. | 27 built-in adapter roots are defined. |
| Configured sources | A supported adapter root exists, or a user root was accepted from `~/.algo_cli/harness_roots.json`. This does not mean it is enabled or indexed. | 14 adapter roots are available: 9 Codex and 5 OpenClaw. No extra roots are configured. |
| Currently indexed sources | Records from an enabled source are present in the current generated harness index. | Zero external records; `indexed_harnesses` is empty. |
| Runtime-disabled sources | A source may be supported and present on disk but excluded by the active privacy setting. | `external_agent_stores=false`; every external adapter is disabled for retrieval. |

The snapshot above came from `/harness status` in Algo CLI v0.18.0 at the verification revision. It is not a standing availability promise. Before claiming that any external store is available, run `/harness status` in the exact active runtime and verify all of the following:

1. `context_sources.external_agent_stores` is `true`.
2. The expected adapter appears under `available_by_harness`.
3. The expected harness appears under `indexed_harnesses` with a non-zero external record count.
4. Extra roots, if any, are accepted rather than rejected.
5. The selected provider is allowed to receive the retrieved content.

Use `/harness refresh` after changing source configuration, then run `/harness status` again. Every record retains its harness, kind, path, relative path, and update time. Conflicting records from different harnesses remain available with provenance; only duplicate harness/kind/relative-path records are collapsed in stable source order.

An absent directory is an unavailable adapter, not a runtime failure. A malformed `harness_roots.json` is degraded configuration and must not silently suppress otherwise valid entries. Correct the file, confirm directory permissions, refresh, then verify record counts by harness before relying on retrieved evidence.

Never add credential directories, tokens, or broad home-directory roots. Retrieved local text can be sent to the active inference provider.
