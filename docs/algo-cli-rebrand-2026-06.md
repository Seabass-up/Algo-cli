# Algo CLI Rebrand — June 2026

**Status:** Complete  
**Version:** 0.3.0  
**Date:** 2026-06

## Summary

The project formerly known as **ollama-cli** has been fully rebranded to **Algo CLI**.

This was executed in three deliberate phases to minimize user disruption:

### Phase 1 — Structural Rename (Commit 580c812)
- Package renamed from `ollama_cli` to `algo_cli`
- Command renamed from `ollama-cli` to `algo-cli`
- Dual entry points registered so the old `ollama-cli` command continues to work as a shim
- Version bumped to 0.3.0
- All imports, launchers, pyproject.toml, and tests updated

### Phase 2 — Default Directory & Migration (Commit dbcdb64)
- `~/.algo_cli` is now the canonical default config directory
- Automatic one-time migration from legacy `~/.ollama_cli`
- Full backup created at `~/.ollama_cli.backup`
- Sentinel file `.migrated_from_legacy` prevents re-running migration
- Strengthened user messaging during migration
- Legacy `OLLAMA_CLI_*` environment variables still supported (with deprecation notice)

### Phase 3 — Visible Branding Sweep (Commit 850fff9)
- All user-facing strings updated ("Ollama CLI" → "Algo CLI")
- Banner, `--help`, system prompts, error messages, and status text refreshed
- README, CLAUDE.md, AGENTS.md, and CHANGELOG updated
- Internal harness source roots, cache IDs, and skill tags updated to `algo-cli`
- Positioning language changed from "Ollama-first" to "Local-first agentic terminal assistant"

## Current Technical Identity

| Aspect                  | Value                          |
|-------------------------|--------------------------------|
| Command                 | `algo-cli`                     |
| Legacy shim             | `ollama-cli` (1 release)       |
| Python package          | `algo_cli`                     |
| Default config dir      | `~/.algo_cli`                  |
| Legacy config dir       | `~/.ollama_cli` (auto-migrated)|
| Env prefix              | `ALGO_CLI_*`                   |
| Legacy env prefix       | `OLLAMA_CLI_*` (still read)    |
| PyPI name               | `algo-cli`                     |

## Backward Compatibility Window

- The `ollama-cli` command remains registered
- `OLLAMA_CLI_*` environment variables are still honored
- Old config directory contents are automatically copied on first run of the new binary
- Users will see clear migration and deprecation messages

This compatibility layer is intended to last for one minor release cycle.

## Harness & RAG Impact

- This repository now surfaces under the `algo-cli` source root in the harness
- Crystallized skills are written to `~/.algo_cli/skills/`
- When using `harness_search`, `harness_read`, or RAG injection, queries for "algo cli", "algo-cli", or "algocli" will surface current information

## Recommended Memory / Wiki Entry

```
The terminal coding agent I primarily use is **Algo CLI** (command: `algo-cli`).

It is a local-first agentic terminal assistant supporting:
- Local Ollama models
- Ollama Cloud
- xAI Grok (via OAuth)

Key capabilities:
- Strong identity layer (SOUL.md, IDENTITY.md, USER.md, lessons-learned.md)
- Harness RAG over multiple agent ecosystems
- Opt-in, local-only skill crystallization
- Structured tool use with approval gates and safe mode
- Context compression, pruning, and reflection checkpoints

Default configuration lives at `~/.algo_cli/`.

It was previously known as ollama-cli. The rebrand completed in June 2026 (v0.3.0) across three phases (structural rename, migration, branding).

Legacy `ollama-cli` command and `OLLAMA_CLI_*` variables continue to work during the transition period.
```

## Links

- Commit 580c812 — Structural rename
- Commit dbcdb64 — Migration & default directory
- Commit 850fff9 — Branding sweep

---

**Last Updated:** 2026-06  
**Maintained by:** The Algo CLI project
