---
name: memory-recall-ritual
description: Capability-aware checks for durable memory, indexed guidance, and an optional knowledge graph when they are relevant to a task.
tags: [algo-cli, memory, harness, knowledge-graph, ritual]
created: 2026-06-09
---

# Memory Recall Ritual

## Trigger

When prior user preferences, packaged guidance, or configured project knowledge
could materially change the task. Skip this ritual for simple direct reads and edits.

## Steps

1. **Use already-loaded memory.** Long-term memories and relevant lessons are
   already included in the system context. Treat them as hints and verify
   consequential facts.
2. **Search indexed guidance when relevant.** Call `harness_stats`, then
   `harness_search(query="<topic-relevant terms>")`; read only the strongest
   records with `harness_read`.
3. **Use the graph only when enabled.** Check `/icl status`. Call
   `query_knowledge_graph("<topic>")` only when index-compute-lab is enabled
   and ready; never enable or rebuild it implicitly.
4. **Plan from evidence.** State what is known, what remains unverified, and
   the next narrow action.

## Key Discoveries

- `harness_search` returns RAG snippets, not full files. When the snippet
  is too small to be useful, call `harness_read(record_id)` to get the
  full body. Record ids look like `codex:skill:browser-qa/SKILL.md`.
- The `~/.algo_cli/memory.json` file is a flat list of facts loaded directly
  into the system context; it is not made searchable merely by refreshing the
  harness.
- `update_user_profile` writes to `~/.algo_cli/identity/USER.md`. This
  file is prepended to every system prompt. Use it for stable
  user-specific facts (role, location, communication style), not for
  transient session notes (use `remember` for those).
- `query_knowledge_graph` is an optional index-compute-lab integration. If it
  is disabled or unavailable, continue without it unless the user explicitly
  requests setup or a rebuild.
- The Mercury compact stop-conditions are injected on every turn
  regardless of session mode. They are the "be careful about X" rule
  the model should never ignore. They are NOT user instructions and
  NOT proof that files exist.

## Environment

Algo CLI only. External harness stores and index-compute-lab remain opt-in.
