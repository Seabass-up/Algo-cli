## Rebrand to Algo CLI Completed (June 2026)

### What Happened
The project formerly known as **ollama-cli** completed a full product rebrand to **Algo CLI** in June 2026.

This was executed as three deliberate, low-risk phases:

- **Phase 1 (580c812)**: Structural rename — package `ollama_cli` → `algo_cli`, command `ollama-cli` → `algo-cli`, dual entry points added for compatibility, version bumped to 0.3.0.
- **Phase 2 (dbcdb64)**: Migration flip — `~/.algo_cli` became the new default. Automatic one-time copy from legacy `~/.ollama_cli` with full backup at `~/.ollama_cli.backup`. Sentinel file prevents re-migration.
- **Phase 3 (850fff9)**: Visible branding sweep — all user-facing strings, banner, help text, system prompts, and documentation updated. Positioning changed from "Ollama-first" to "Local-first agentic terminal assistant".

### Key Outcomes
- New canonical locations: `~/.algo_cli/`, `ALGO_CLI_*` environment variables, `algo_cli` Python package.
- Strong backward compatibility maintained during transition (legacy command and env vars still work with deprecation notices).
- Harness RAG and skill crystallization now correctly surface under the `algo-cli` source root.

### Lessons Learned
- A major rebrand can be executed safely when dual-support and automatic (but guarded) migration are implemented *before* changing defaults.
- Phased commits (Structural → Migration → Branding) made the work reviewable and low-risk.
- Historical documentation (CLAUDE.md, AGENTS.md, CHANGELOG) requires dedicated cleanup time after a rebrand.
- Internal backend function names (e.g., anything talking directly to the Ollama SDK) should **not** be renamed — only product-facing identity changes.
- Power users with large multi-agent setups greatly appreciate clear migration messages and explicit backup locations.

### Recommendations
- When performing future rebrands or major renames, always prioritize a compatibility window + automatic safe migration.
- Generate ready-to-use memory/wiki entries early for heavy users of the harness RAG system.
- Test the legacy shim path explicitly during final validation.

**Date:** 2026-06  
**Related Commits:** 580c812, dbcdb64, 850fff9

---

*This lesson should be retained as it affects how we approach future tool identity and configuration changes.*