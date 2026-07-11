# Algo CLI skills

This directory contains the public, packaged skills that Algo CLI can retrieve through its harness. They are available in both source checkouts and installed wheels.

| Skill | Purpose |
|---|---|
| `algo-cli.md` | Product capabilities, configuration, and migration facts. |
| `edit-file-precision.md` | Precise file edits and common recovery paths. |
| `harness-search-first.md` | Choosing indexed retrieval before broad scans. |
| `memory-recall-ritual.md` | Capability-aware memory and retrieval checks. |
| `qol-algorithms.md` | Small algorithms used on CLI hot paths. |
| `smart-error-recovery.md` | Classify failures before choosing a retry. |
| `tool-selection-cheatsheet.md` | Pick the narrowest suitable runtime tool. |

## Sources and privacy

Packaged skills and user-created records under `~/.algo_cli` are available by default. Other local agent stores—Codex, Claude, OpenClaw, Mercury, Pi, CLI Agent, and shared `.agents`—are excluded until the user runs `/harness external on`. Retrieved context becomes part of the active model request, so opt-in sources may leave the machine when a cloud provider is selected.

## Adding a skill

1. Use neutral examples and the frontmatter conventions in an existing skill.
2. Save the new Markdown file in this directory.
3. Run `/harness refresh`.
4. Verify retrieval with `harness_search(query="distinctive terms", kind="skill")` and `harness_read(record_id)`.

User-generated skills belong under `~/.algo_cli/skills`. External skills do not need to be copied after the corresponding source has been explicitly enabled.
