---
status: historical
tags: [reflex, design-history]
---

# Reflex Loop Spec v0.2

**Status:** Historical design contract; retained for provenance, not current runtime guidance.
**Origin:** derived from the v0.1 in-conversation draft and the 2026-06 session notes on self-healing reflex attempts.
**Provenance note:** the spec is the reflex's *own* contract. It composes existing skills; it does not reimplement them.

---

## §1 Purpose

A thin in-session orchestrator that detects when the current approach is failing, decides
whether recovery is safe, applies the smallest contained corrective action, and verifies
the recovery actually worked. The reflex is **not** a runtime feature. It is a workflow
contract that an Algo CLI or compatible agent follows inside an
existing session.

The reflex composes three existing skills:

| Phase | Composed skill | Role |
|---|---|---|
| DETECT / DECIDE / ACT | `codex:skill:agent-introspection-debugging` | four-phase self-debug loop |
| VERIFY (code-change mode) | `codex:skill:verification-loop` | post-change quality gate |
| VERIFY (index / prefilter mode) | harness verify pass + confidence gate | post-mutation index check |

The reflex adds three things the composed skills do not own:

1. A **session-scoped attempt ledger** that prevents the same failed action from being
   retried with cosmetic variation.
2. A **safe-allowlist** that constrains which tools the ACT step may call, derived from
   the introspection skill's "do not claim unsupported auto-healing actions" rule.
3. A **state machine** that gives the loop an exit condition, a budget, and a single
   `resolved:true` terminal signal.

Everything else is inherited.

---

## §2 Triggers

The reflex activates when at least one of the following is true:

1. **Same tool, same arguments, third call.** (Loop detector — catches `harness_search`
   for the same query, `read_file` on the same path, etc.)
2. **Safe-mode block on a non-destructive-looking command.** (Catches destructive
   operations that slipped past the verb classifier — `Move-Item`, `del /s`, etc.)
3. **Tool result has zero useful lines and the action was repeated once already.**
   (Catches dead-end reads / searches / web fetches.)
4. **In-session attempt ledger hit count > 2 for the same `action_class`.** (Belt and
   suspenders for #1; useful when the same verb is used with different args.)
5. **Explicit human request.** ("Use the reflex on this." / "self-heal this.")

**Removed from v0.1:** trigger #4 was "in-conversation self-heal mechanism." It duplicated
the explicit-request trigger and added no signal.

**Session-level cap:** the reflex may run at most **3 reflex cycles per session** without
explicit human approval for further cycles. A 4th attempt must `ESCALATE` rather than
re-enter DETECT.

---

## §3 State machine

```
            ┌──────────┐
            │ DETECT   │  -- agent-introspection-debugging Phase 1 (Capture)
            └────┬─────┘
                 │ capture_record
                 ▼
            ┌──────────┐
            │ DECIDE   │  -- agent-introspection-debugging Phase 2 (Diagnosis)
            └────┬─────┘
                 │ diagnosis_record
        ┌────────┴────────┐
        │                 │
   resolution =       resolution =
   "act"              "escalate" / "no_act"
        │                 │
        ▼                 ▼
   ┌──────────┐      ┌────────────┐
   │ ACT      │      │ ESCALATE   │  (terminal)
   └────┬─────┘      └────────────┘
        │ action_record
        ▼
   ┌──────────┐
   │ VERIFY   │  -- mode chosen below
   └────┬─────┘
        │
   ┌────┴────────────────────┐
   │                         │
   mode = "code_change"      mode = "index_or_prefilter"
   │                         │
   calls verification-loop   calls harness verify pass
   reads READY/NOT READY     applies confidence gate (2 of 3)
   │                         │
   └────┬────────────────────┘
        │
   ┌────┴───────────┐
   │                │
   resolved=true   verify_uncertain / verify_failed
   (terminal)            │
                        ▼
                   back to DETECT  (with cap guard)
                   OR ESCALATE     (if cap hit)
```

**Edge rules:**

- A cycle that ends in `ESCALATE` consumes the session cap.
- A cycle that ends in `resolved:true` does **not** reset the cap; cap counts are
  cumulative across the session.
- The `back to DETECT` edge from VERIFY is permitted **only** if the cycle counter is
  below the cap and the verify outcome is `verify_uncertain` (not `verify_failed`).
  `verify_failed` is treated as `ESCALATE` regardless of cap state, because a failed
  verify on a recovery is a strong signal the diagnosis was wrong.

---

## §4 Class taxonomy (inherited, not invented)

The v0.1 spec invented a small class list (`stale_index`, `dup_index`, `budget_exhausted`,
`quality_gate_failure`). v0.2 drops that list in favor of the pattern table already
maintained by `agent-introspection-debugging`, with a **mapping column** for reflex-specific
classes.

The inherited table is the source of truth. Reflex classes are labels, not categories.

| Pattern (inherited) | Likely cause (inherited) | Check (inherited) | Reflex class label(s) |
|---|---|---|---|
| Maximum tool calls / repeated same command | loop or no-exit observer path | inspect last N tool calls for repetition | `loop_detected` |
| Context overflow / degraded reasoning | unbounded notes, repeated plans, oversized logs | inspect recent context for duplication and low-signal bulk | `stale_index`, `dup_index`, `context_overflow` |
| `ECONNREFUSED` / timeout | service unavailable or wrong port | verify service health, URL, and port | `service_unavailable` |
| `429` / quota exhaustion | retry storm or missing backoff | count repeated calls and inspect retry spacing | `budget_exhausted`, `retry_storm` |
| File missing after write / stale diff | race, wrong cwd, or branch drift | re-check path, cwd, git status, actual file existence | `stale_diff`, `path_drift` |
| Tests still failing after "fix" | wrong hypothesis | isolate the exact failing test and re-derive the bug | `quality_gate_failure` |

**Why labels, not categories:** the introspection skill's diagnosis is pattern-based and
may map to multiple reflex class labels. Labels are what the attempt ledger records; the
diagnosis is what the introspection skill produces. The two stay separate.

**Diagnosis precedence (from introspection skill, paraphrased):** before changing anything,
answer these in order:

1. Is this a logic failure, state failure, environment failure, or policy failure?
2. Did the agent lose the real objective and start optimizing the wrong subtask?
3. Is the failure deterministic or transient?
4. What is the smallest reversible action that would validate the diagnosis?

If #2 is yes, the reflex must ESCALATE — the diagnosis question takes priority over the
class label.

---

## §5 Safe allowlist

The ACT step may call only the following, in this order of preference:

1. `harness_search` / `harness_read` (read-only observation, no mutation)
2. `run_shell` with **non-mutating verbs only** (`Get-*`, `Test-*`, `Select-String`,
   `rg`, `ls`, `cat`, `head`, `tail`, `find`, `stat`)
3. `read_file`, `search_files`, `list_directory`
4. `git_status`, `git_diff` (read-only git ops)
5. `web_search`, `web_fetch` (external research)

**Governing principle (lifted from `agent-introspection-debugging`):** do not claim
auto-healing actions like "reset agent state," "update harness config," or "rewrite
the index" unless the agent is actually performing them through a real tool call in
the current environment. The safe allowlist is a strict subset of what the agent can
do; it is not a description of what the agent wishes it could do.

**If the diagnosis requires an off-list action, the reflex must ESCALATE.** Examples
that would require off-list actions (and therefore escalate, not act):

- The fix needs a destructive `Move-Item` / `del` — escalate.
- The fix needs to mutate `harness_index.json` directly — escalate to
  `workspace-surface-audit` (per the introspection skill's integration note).
- The fix needs to push to a remote, install a package, or spawn a background process —
  escalate.

**Off-list ESCALATE routing** is delegated to whichever existing skill owns the domain
(per the introspection skill's "use the narrower ECC skill when one exists" rule). The
reflex does not invent routing.

---

## §6 Verify

The VERIFY step is two-mode. The mode is chosen by inspecting the ACT step's action
record.

### Mode A: `code_change`

If the ACT step emitted a code change (file write, file edit, package install, config
change), the reflex uses the enabled verification workflow. It may use
`codex:skill:verification-loop` when external Codex sources are explicitly enabled
and that record is present; otherwise it falls back to the built-in verification gate.

| Verdict | Reflex behavior |
|---|---|
| `READY` | `resolved:true`, write ledger, end cycle |
| `NOT READY` | `verify_failed`, ESCALATE (per §3 edge rules) |
| `verification-loop` itself fails or is unavailable | `verify_uncertain`, fall through to Mode B if applicable, else ESCALATE |

The reflex consumes the **verdict**, not the full report. The full report goes to the
human-readable introspection output.

### Mode B: `index_or_prefilter`

If the ACT step was a prefilter invocation, a query re-issuance, or a read-only
patch to a script that will be run separately, the reflex does its own confidence
gate. The gate passes (returns `resolved:true`) when **at least two of the following
three are true**:

1. The top-hit `path` field in the new `harness_search` result differs from the
   previous top hit.
2. The cosine-similarity **delta** between the previous and new top hit (using the
   harness RAG's `_cosine` over the `embedding` field — see
   `algo_cli/harness.py:_cosine`) is **≥ 0.05**.
3. The dropped-record count in the new prefilter report matches the count the
   prefilter said it would drop.

If fewer than two pass, the reflex records `verify_uncertain` and either re-enters
DETECT (under cap) or ESCALATES.

**Provenance and open tuning.** The 0.05 delta in item 2 is a **starting value**,
not an inherited threshold. It does **not** come from `harness_prefilter.py`,
whose similarity gate is `_token_jaccard(...) >= DEDUPE_SIM_THRESHOLD` (0.92,
absolute, on titles within the same record group). That gate answers a
different question (is this record a dupe?) than Mode B is asking (did the
prefilter actually change the top hit the user is looking at?). Conflating them
was a v0.2 drafting error. The first real session that exercises Mode B should
log the observed delta distribution; if the median delta is far from 0.05, the
threshold moves and this section is updated.

### `verify_mode: false` sessions

When the user has set `verify_mode: false` for the session (a future flag, not yet
implemented), DETECT and DECIDE still run. DECIDE's output is constrained to
`escalate` or `no_act` — it cannot transition to ACT. This is the safe-mode for
healing behavior the user has explicitly disabled.

---

## §7 Integration matrix

| Phase | Skill | Path | Confidence |
|---|---|---|---|
| DETECT | `agent-introspection-debugging` | user-configured skill root | read this turn — grounded |
| DECIDE | `agent-introspection-debugging` | (same) | grounded |
| ACT | `agent-introspection-debugging` (Phase 3) + safe allowlist (§5) | (same) | grounded, with the allowlist as the reflex's local addition |
| VERIFY (code_change) | `verification-loop` | user-configured skill root | read this turn — grounded |
| VERIFY (index_or_prefilter) | harness verify pass + confidence gate | n/a (built into reflex) | grounded |
| crystallize destination | **no fixed consumer** | label only | **referenced-but-absent** — see Unverified Claims below |
| off-list ESCALATE (state / repo drift) | `workspace-surface-audit` (per introspection integration note) | not yet located in harness | unverified path |
| off-list ESCALATE (decision ambiguity) | `council` (per introspection integration note) | not yet located in harness | unverified path |

**`continuous-learning-v2` status (revised this turn):** the skill named in
`agent-introspection-debugging`'s integration section does **not** exist in the harness
index. The closest functional neighbors are `codex-session-skill-mining` and
`knowledge-ops`, but neither is a drop-in replacement. v0.2 does not bind a fixed
consumer — the reflex emits a `crystallize_candidate` label and the human routes it.

---

## §8 Outcomes

Each cycle terminates in one of:

| Outcome | Meaning | Terminal? |
|---|---|---|
| `resolved:true` | Recovery succeeded and verified | yes |
| `escalated` | Off-list action required, or cap hit, or `verify_failed` | yes (human decision) |
| `blocked-by-safe-mode` | ACT was called but a tool in the allowlist was blocked by the shell safe mode | yes (per-action, not terminal for the session) |
| `verify_uncertain` | VERIFY could not produce a confident yes/no | no (re-enters DETECT under cap) |
| `no_act` | DECIDE determined the right action is "do nothing; restate hypothesis" | yes |

A `no_act` outcome is not a failure. The introspection skill's first recovery heuristic
is "restate the real objective in one sentence." Sometimes the right answer is to stop
and restate, not to mutate.

---

## §9 Persistence

- **In-session:** the attempt ledger is a runtime record. It lives in the agent's
  working memory for the session. There is no filesystem write to a ledger file.
- **Cross-session:** the **only** durable record is the human-readable
  "Agent Self-Debug Report" produced at the end of the cycle (per the introspection
  skill's Phase 4) and, optionally, a one-line append to the user's `lessons-learned.md`
  for patterns that recur.
- **Claim:** the v0.1 spec suggested a JSON ledger file with `resolved:true`. v0.2
  rejects that. JSON ledgers are an anti-feature for a workflow that runs once per
  cycle; they invite stale data and write-amplification. The introspection report is
  the ledger. If structured persistence is needed later, the right move is a new
  skill that *crystallizes* the report, not a side-channel file.

---

## §10 Open items → appendices

The v0.1 spec ended with a §10 "open items" list. v0.2 splits the resolved and
unresolved claims into two appendices.

### Appendix A — Resolutions

| # | v0.1 issue | v0.2 resolution |
|---|---|---|
| 1 | Two-on-the-same-tool vs. three-on-the-same-tool | Three-call threshold; second call warns in-line, third triggers DETECT |
| 2 | "In-conversation self-heal mechanism" trigger | Removed; folded into explicit-request trigger |
| 3 | Budget / session cap | 3 cycles per session, 4th must ESCALATE |
| 4 | Class taxonomy duplication with `agent-introspection-debugging` | Inherited that skill's pattern table; reflex classes are labels, mapped 1:N |
| 5 | VERIFY one-size-fits-all | Two-mode VERIFY (§6) |
| 6 | "do not claim unsupported auto-healing" framing | Imported as §5 governing principle |
| 7 | Off-list actions | §5 — escalate, do not invent a permit |
| 8 | Confidence gate for index-mode verify | §6 Mode B — two-of-three gate |
| 9 | Crystallize dedup ownership | §7 — label only, no fixed consumer (skill is absent) |
| 10 | `harness_healthcheck.py` | Dropped; reflex does not own healthcheck, can call one when available |
| 11 | Schema example for `escalated` / `blocked-by-safe-mode` | §8 — full outcome table |
| 12 | `resolved:true` cross-session persistence | §9 — rejected, in-session only |
| 13 | Off-list ESCALATE routing | §5 / §7 — delegate to existing skills; reflex does not own routing |
| 14 | Missing outcome types | §8 — five outcomes |

### Appendix B — Unverified Claims

| Claim | Why flagged | Action |
|---|---|---|
| `continuous-learning-v2` is a real skill | Referenced in `agent-introspection-debugging` integration section; not in harness index | v0.2 treats it as absent; label-only contract |
| `workspace-surface-audit` path | Referenced in same section; not located this turn | Off-list ESCALATE that needs it must locate it first; do not blind-route |
| `council` path | Same | Same |
| `codex-session-skill-mining` and `knowledge-ops` cover the crystallize use case | Both mention dedup / skill creation in their summaries; not yet read end-to-end | Acceptable as a downstream hint; not a reflex contract |
| `verify_mode: false` flag | Referenced in §6; not implemented | Reserved for a future v0.3, not a v0.2 commitment |
| Cosine-similarity delta 0.05 in §6 Mode B item 2 | Was mislabelled as inherited from the prefilter; the prefilter's gate is Jaccard 0.92 absolute. 0.05 is a starting delta on the harness RAG's cosine (`algo_cli/harness.py:_cosine`). | Log real deltas in the first session that exercises Mode B; recalibrate. |

---

## End of v0.2

Next moves the spec suggests, in order:

1. Read `codex-session-skill-mining` end-to-end to confirm or rule out the
   `continuous-learning-v2` claim. One tool call.
2. (Done 2026-06 turn.) §6 Mode B's threshold provenance was wrong — the
   0.05 is a starting delta on the harness RAG's cosine, not the prefilter's
   Jaccard gate (which is 0.92 absolute). Section updated; threshold itself
   stays at 0.05 pending observed-delta calibration.
3. After both, write a one-page operator's guide — the spec is the contract,
   the guide is the "how do I invoke this" doc.
4. Hand the `edit_file` tool spec to the reflex as a follow-up. v0.1 deferred this
   because the integration model was wrong; v0.2's safe allowlist and two-mode
   VERIFY now make the tool's contract clearer.
