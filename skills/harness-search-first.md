---
name: harness-search-first
description: Before broad filesystem scans, call harness_search to find skills, prompts, memory, wiki pages, and workflows already indexed from codex/claude/openclaw/mercury/pi/agents.
tags: [algo-cli, harness, RAG, navigation]
created: 2026-06-09
---

# Harness Search First

## Trigger

When the user asks for "the X skill" or "how to do Y" or wants a known
workflow, or when you are about to scan a large directory tree, run
`harness_search` first.

## Steps

1. Call `harness_search(query, harness_name=None, kind=None, limit=10)`.
2. If results exist, pick the most relevant 1-3 and call
   `harness_read(record_id)` to load their full body.
3. If no results match, fall back to `search_files` and `read_file`.
4. When writing your final answer, cite the harness record ids you used
   (e.g. "codex:skill:browser-qa/SKILL.md") so the user can find the source.

## Key Discoveries

- The harness can cover **seven** external harnesses when the user opts in:
  codex, claude, openclaw, agents, mercury, cli-agent, pi. Their skills
  flow into `harness_search` without any per-harness plumbing.
- `harness_name` accepts aliases: `openclaude` -> {claude, openclaw},
  `codex-cli` -> {codex}, `all` -> no filter. Use these for natural-language
  routing.
- `kind` is one of: skill, tool, prompt, memory, wiki, workflow, extension.
  Most retrieval questions are `skill` or `wiki` — start there.
- For configured project/entity memory questions, prefer `query_knowledge_graph`
  when the user has enabled index-compute-lab; it uses ranked associations and
  can be faster and more relevant than raw `harness_search`.
- The harness index is mtime-cached in `~/.algo_cli/harness_index.json`.
  After editing a skill file, call `harness_refresh` to update the index
  immediately (otherwise the next /harness refresh will pick it up via mtime).

## Environment

algo-cli only. The harness index is built and maintained by
`algo_cli/harness.py:build_index()`. Rust indexer at `harness-indexer/`
is an optional speedup for cold-start builds but the Python scanner is
authoritative.
