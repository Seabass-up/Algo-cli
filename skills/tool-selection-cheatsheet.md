---
name: tool-selection-cheatsheet
description: Quick reference for picking the right tool — read vs search vs harness_search, edit_file vs write_file, run_shell vs session_command.
tags: [algo-cli, tool-selection, cheatsheet, productivity]
created: 2026-06-09
---

# Tool Selection Cheatsheet

## Trigger

When you are about to call a tool and are not 100% sure which one to use.

## Steps

1. If you are looking for **a known file** by path → `read_file` (or
   `session_slash /read` for deterministic cwd-relative).
2. If you are looking for **a fact or string** inside any file →
   `search_files` for direct grep, OR `harness_search` first if the
   fact might be in a skill/memory/wiki.
3. If you are looking for **a workflow or how-to** → `harness_search
   (kind="skill")` first. Use `query_knowledge_graph` for project/entity
   relations only when `/icl status` reports that integration enabled and ready.
4. If you need to **change a single specific thing in an existing file**
   → `edit_file` (find/replace) with 2-5 lines of context.
5. If you need to **create a new file** → `write_file` (no overwrite).
6. If you need to **rewrite most of an existing file** → `write_file` with
   `overwrite=True` after re-reading the target.
7. If you need to **run tests/builds/lint/diff/grep** → `run_shell` (safe
   in requires_change blocks; mutations are flagged for approval).
8. If you need to **change session state** (model, theme, context,
   harness refresh, route preview) → `session_command` (`/status`,
   `/mode execute`, `/context status`, `/harness refresh`, `/route TASK`).
9. If you need to **fetch a URL or search the web** → `web_search` /
   `web_fetch` (requires OLLAMA_API_KEY for Ollama Cloud).
10. If you are **unsure what to use** → `available_actions(topic="...")`
    returns the relevant tools + slash commands for any focus area.

## Key Discoveries

- `edit_file` is preferred over `write_file` for ANY change to an existing
  file. The cost is the same, but edit_file reports the affected line
  range, fails on ambiguous matches, and uses fewer tokens (you don't
  have to read+echo the whole file).
- `session_slash` is preferred over `read_file` when the path is
  cwd-relative and the user named the file in a `/read`-style request.
  It honors `/cd` state.
- `query_knowledge_graph` can be faster and more relevant than
  `harness_search` for project/entity questions when the user enabled and
  populated index-compute-lab. Use it for relationship questions about entities.
- `harness_search` is preferred over `search_files` when the file
  might be in a skill/memory/wiki, because the indexer has already
  parsed frontmatter and scored relevance. `search_files` is the
  right call for raw code/text/grep across the workspace.
- `available_actions(topic="...")` is the meta-tool: if you find
  yourself guessing, call it. Topics: files, shell, web, memory,
  harness, models, reasoning, slash, documents, multimodal.
- `run_shell` mutations are audited and may be blocked. The `requires_change`
  block tells you which shell commands count as mutations; everything
  else (read-only) is fine.

## Environment

algo-cli >= 0.4. The tool inventory in `available_actions` is the
authoritative list; this skill captures the selection heuristics.
