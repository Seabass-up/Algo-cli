---
name: qol-algorithms
description: Quality-of-life algorithm patterns for Algo CLI that reduce friction, prevent common errors, and improve terminal UX.
tags: [algo-cli, qol, algorithms, ux, fuzzy-matching, configuration, suggestions]
created: 2026-07-02
---

# Quality of Life (QoL) Algorithms

A compact catalog of medium-priority UX algorithms for Algo CLI. They don't change retrieval correctness, but they make the terminal feel faster, safer, and more forgiving.

## 1. Fuzzy Slash-Command Matcher

**Use for:** turning typos like `/selfcehck`, `/harnes`, or `/modle-check` into "Did you mean `/selfcheck`, `/harness`, `/model-check`?"

**Algorithm:**

```text
candidates = all registered slash commands + aliases
for candidate in candidates:
    d = Damerau-Levenshtein distance(query, candidate)
    score = 1 - d / max(len(query), len(candidate))
rank by score descending, filter score >= 0.6
```

**Why it matters:**

- Users type fast in a terminal; a single transposition shouldn't fail silently.
- Damerau-Levenshtein handles adjacent swaps (`selfcehck` → `selfcheck`) better than plain Levenshtein.
- A ranked suggestion is safer than silent auto-correction.

**Harness contract:**

- Input: mistyped slash command string, registered command list.
- Output: zero or more suggestions with confidence scores.
- Telemetry: typo count, accepted suggestion, fallback to unknown-command handler.

**Tests:**

- `/selfcehck` suggests `/selfcheck` with score >= 0.8.
- `/harnes` suggests `/harness` before `/harness-search`.
- `/totally-unknown` returns no suggestions and falls through.
- Suggestions are deterministic for a fixed command registry.

## 2. Layered Configuration Precedence

**Use for:** resolving defaults, config file values, environment variables, and per-command flags without surprising overrides.

**Precedence (lowest to highest):**

```text
built-in default
config file (algo_cli/config.json)
environment variable (ALGO_CLI_*)
per-command flag / runtime override
```

**Why it matters:**

- Smart defaults reduce setup friction.
- A clear precedence order prevents "why didn't my env var work?" bugs.
- Makes telemetry reproducible: log the winning source for each setting.

**Harness contract:**

- Input: setting name, default value, config dict, env mapping, CLI overrides.
- Output: resolved value plus source provenance.
- Telemetry: override source counts, unknown config keys.

**Tests:**

- Default wins when no other source provides the value.
- Env var overrides config file.
- Runtime flag overrides env var.
- Unknown config keys are warned, not silently ignored.

## 3. Progressive Command Aliases

**Use for:** letting users type short, memorable forms (`/hs` for `/harness-search`, `/m` for `/model`) without fragmenting the command namespace.

**Algorithm:**

```text
alias_map = {
    "hs": "harness-search",
    "m":  "model",
    ...
}
resolve(input):
    if input in alias_map: return alias_map[input]
    if input is a registered command: return input
    else: pass to fuzzy matcher
```

**Why it matters:**

- Reduces keystrokes for power users.
- Keeps help text and telemetry canonical by expanding aliases early.
- Avoids the trap of many near-duplicate commands.

**Harness contract:**

- Input: raw slash command token.
- Output: canonical command name or unknown.
- Telemetry: alias expansion count, collisions.

**Tests:**

- `/hs` resolves to `/harness-search`.
- Unknown alias falls through to fuzzy matcher.
- Alias-to-alias chains are flattened or rejected.
- Help text lists aliases next to canonical names.

## 4. History-Aware Command Suggestions

**Use for:** surfacing likely next commands based on the current session's command history.

**Algorithm:**

```text
score(cmd) = recency_weight * last_used_seconds_ago^-1
           + frequency_weight * count_in_session
           + context_weight * co_occurrence_with_last_cmd
return top-k, deduplicated
```

**Why it matters:**

- Repeating a recently used `/harness-search` or `/model` is common.
- Recency + frequency beats either signal alone.
- Context boost helps after multi-step workflows (e.g., `/google-login` → `/google-status`).

**Harness contract:**

- Input: session command history, current command, k.
- Output: ranked suggestion list.
- Telemetry: suggestion acceptance rate, history length.

**Tests:**

- Most recent unique command appears first.
- Frequent but stale command is ranked below recent frequent command.
- Suggestions exclude the command just typed.
- Empty history returns defaults or nothing.

## 5. Confidence-Gated Auto-Correction

**Use for:** automatically fixing low-risk typos (command names, common flag values) while asking the user when confidence is low.

**Algorithm:**

```text
if best_suggestion.score >= high_threshold (e.g. 0.85):
    auto-correct and run
elif best_suggestion.score >= low_threshold (e.g. 0.60):
    prompt user: "Did you mean X? [y/n]"
else:
    report unknown command
```

**Why it matters:**

- High-confidence corrections save keystrokes.
- Low-confidence guesses are destructive if applied silently.
- A threshold makes the behavior testable and tunable.

**Harness contract:**

- Input: raw input, suggestion list, high/low thresholds.
- Output: corrected input, prompt, or error.
- Telemetry: auto-correct count, prompt count, rejection count, threshold breaches.

**Tests:**

- Score 0.90 auto-corrects without prompt.
- Score 0.70 prompts user.
- Score 0.40 reports unknown.
- Thresholds are configurable per command class.

## 6. Mojibake Fixer

**Use for:** cleaning UTF-8 display artifacts (`Â·`, `â€¦`, `â€™`, `â€œ`) in harness output.

**Algorithm:** Detect windows-1252 mojibake in otherwise UTF-8 text and replace the byte sequence with the intended Unicode glyph.

**Tests:**

- `algo-cli Â· wiki Â·` becomes `algo-cli · wiki ·`.
- `periodicâ€¦` becomes `periodic…`.
- `donâ€™t` becomes `don't`.

## 7. Relevance Threshold Filter

**Use for:** suppressing `## Relevant Context` blocks when none of the top-k records are actually relevant.

**Algorithm:**

```text
filtered = [r for r in ranked if r.score >= min_relevance]
```

**Tests:**

- If all scores < 0.3, return empty list.
- If top record > 0.7, include all above 0.3.
- Log filtered records for debugging.

## 8. Probe Query Linter

**Use for:** validating `/selfcheck` probe queries are real indexed records.

**Algorithm:** Run each probe query through `harness_search`; mark invalid any query that returns no matches.

**Tests:**

- `index-compute-lab` → valid.
- A retired synthetic label → invalid (no matching record).
- Log invalid queries to suggest replacements.

## References

- Full spec and harness contracts: `docs/ALGO.reviewed.md` Track B.
- Fuzzy matching background: Levenshtein and Damerau-Levenshtein edit distance.
- UX inspiration: fzf, git's DWIM mistyped-command wrapper, smart_config layered defaults.
