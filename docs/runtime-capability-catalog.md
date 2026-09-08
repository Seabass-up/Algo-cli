---
title: Runtime Capability Catalog
description: How ActionSpecs become searchable capability, policy, limitation, and failure-mode records.
tags: [runtime-capability, action-registry, policy, tools, operations]
status: active
updated: 2026-09-07
---

# Runtime Capability Catalog

The ActionSpec registry is the runtime source of truth for tools, slash commands, providers, kernels, and archived actions. Index refresh materializes each ActionSpec as a `runtime_capability` record containing its description, risk, mutation and approval rules, network/provider prerequisites, supported platforms, retry safety, and known limitations.

Discovery uses the effective registry: explicit ActionSpecs plus the generated
coverage records for current tool callables and slash commands. An explicit-only
catalog is incomplete even when all of its own records have embeddings. Generated
records retain the existing curated or unclassified policy; indexing never invokes
the callable or upgrades its authority.

Capability records are discovery evidence, not authority. Retrieving a record never grants permission or bypasses the registry. Bounded execution still applies the live ActionSpec policy and produces normal receipts.

After changing the registry, run `/harness refresh`, complete pending embeddings,
and inspect `/harness status`. The `runtime_capability_coverage` result compares
indexed IDs and policy metadata against the complete effective registry. Missing,
unexpected, duplicate, or stale entries degrade readiness even when embedding
coverage is complete. Indexes from the explicit-only catalog refresh automatically;
unchanged embedding inputs retain their vectors.
