---
title: External Agent Store Operations
description: Opt-in discovery, provenance, conflicts, refresh, and recovery for external harness context.
tags: [harness, external-store, operations, privacy, recovery]
status: active
updated: 2026-07-17
---

# External Agent Store Operations

External stores are disabled by default. Enable them only when local Codex, Claude, OpenClaw, `.agents`, Mercury, CLI Agent, or Pi content may safely enter provider prompts.

Use `/harness status` to inspect adapter availability, indexed harnesses, rejected extra-root entries, and the privacy policy. Use `/harness refresh` after changing source configuration. Every record retains its harness, kind, path, relative path, and update time. Conflicting records from different harnesses remain available with provenance; only duplicate harness/kind/relative-path records are collapsed in stable source order.

An absent directory is an unavailable adapter, not a runtime failure. A malformed `harness_roots.json` is degraded configuration and must not silently suppress otherwise valid entries. Correct the file, confirm directory permissions, refresh, then verify record counts by harness before relying on retrieved evidence.

Never add credential directories, tokens, or broad home-directory roots. Retrieved local text can be sent to the active inference provider.
