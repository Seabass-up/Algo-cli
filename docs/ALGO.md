# ALGO.md

This is the empty public template for your personal Algo algorithm and pattern
catalog. Algo CLI ships without anyone's catalog; you build your own.

## Getting started

1. Run `/intelligence init` in Algo, or copy this file to your Algo config directory:
   `cp docs/ALGO.md ~/.algo_cli/ALGO.md` (or set `ALGO_CLI_CONFIG_DIR` and copy it there).
2. Add one section per pattern you want Algo to know about, using the format below.
3. Run `/harness refresh`, then `/harness patterns status` to confirm Algo parsed your entries.

When `~/.algo_cli/ALGO.md` exists, Algo indexes it instead of this template. Your
catalog stays on your machine and is never part of a release.

## Entry format

Group entries under a track heading (`## Track <letter> — <name>`). Each entry is a
`###` heading with a unique ID, a status line, and whatever notes help you and the
model apply it. Headings inside code fences, like this example, are ignored:

```markdown
## Track A — Retrieval

### A1. Reciprocal Rank Fusion

**Status:** planned

Merge ranked lists by summing 1 / (k + rank). Use when combining lexical and
vector search results.

**Applicability:** {"environments": ["darwin", "linux"], "fallback": "Use the best single ranker."}
```

Valid statuses: `implemented`, `wired`, `partial`, `preview`, `proposed`, `planned`,
`retired`, `unknown`. The optional `**Applicability:**` line is a JSON object with the keys
`environments`, `prerequisites`, `conflicts`, `resource_costs` and `fallback`; see
`algo_cli/pattern_catalog.py` for the validation rules.

## Personal kernels

Kernels are separate from this catalog. Declare your own in
`~/.algo_cli/kernels/kernels.json` and put their Python modules beside that file.
Run `/kernel` in Algo for the exact format.
