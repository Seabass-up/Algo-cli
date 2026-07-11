# Algo CLI — Quick Facts (June 2026)

**Status:** Rebrand Complete (v0.3.0)

## Current Identity
- **Command:** `algo-cli`
- **Legacy shim:** `ollama-cli` (still works for 1 release)
- **Python package:** `algo_cli`
- **Default config directory:** `~/.algo_cli`
- **Legacy config directory:** `~/.ollama_cli` (auto-migrated with backup)
- **Environment variables:** `ALGO_CLI_*` (legacy `OLLAMA_CLI_*` still supported)
- **Positioning:** Local-first agentic terminal assistant

## Key Capabilities
- Strong identity layer (SOUL.md / IDENTITY.md / USER.md / lessons)
- Harness RAG across multiple agent ecosystems
- Opt-in, local-only skill crystallization
- Structured tool use with approval gates + safe mode
- Context pruning, compaction, and reflection checkpoints
- Excellent `--oneshot --json` machine mode

## Performance (June 2026 hot-path pass)

- Single system-prompt build per `agent_loop` iteration (rebuild only after compaction)
- Shared `unpack_embed_response` for slash `/embed` and tool `embed_text`
- O(n) intra-session tool-message pruning
- Context cache respects identity file mtimes
- Harness: no double filesystem walk on cold index; stable embed-batch watermark

Details: `docs/algo-cli-hot-path-perf-2026-06.md`

## Migration Notes
- First run with legacy data automatically copies everything to `~/.algo_cli`
- Full backup created at `~/.ollama_cli.backup`
- Sentinel prevents re-migration
- Clear messages shown during migration

## Recommended Search Terms (for harness / RAG)
`algo cli`, `algo-cli`, `algocli`, `algo_cli`

## Source
- GitHub: https://github.com/Seabass-up/algo-cli
- Primary docs: README.md in source repo
- Detailed rebrand history: `docs/algo-cli-rebrand-2026-06.md`

**Last Updated:** 2026-06

---

*Copy this block into wiki pages, memory systems, or dashboard overviews for quick reference.*
