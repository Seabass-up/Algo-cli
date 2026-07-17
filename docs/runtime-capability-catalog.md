---
title: Runtime Capability Catalog
description: How ActionSpecs become searchable capability, policy, limitation, and failure-mode records.
tags: [runtime-capability, action-registry, policy, tools, operations]
status: active
updated: 2026-07-17
---

# Runtime Capability Catalog

The ActionSpec registry is the runtime source of truth for tools, slash commands, providers, kernels, and archived actions. Index refresh materializes each ActionSpec as a `runtime_capability` record containing its description, risk, mutation and approval rules, network/provider prerequisites, supported platforms, retry safety, and known limitations.

Capability records are discovery evidence, not authority. Retrieving a record never grants permission or bypasses the registry. Bounded execution still applies the live ActionSpec policy and produces normal receipts.

After changing the registry, run `/harness refresh`, complete pending embeddings, and inspect `/harness status`. A zero runtime-capability count is degraded readiness because the harness is no longer self-describing.
