---
name: session-yolo-mode
description: Explain and diagnose user-only YOLO activation, scoped preapproval, and child isolation.
tags: [algo-cli, yolo, session-mode, permissions, B51]
created: 2026-09-16
---

# Session YOLO Mode

Use when the user asks about `/mode yolo` or an action denied in that mode.

1. Inspect `/mode status` and `/status`. Only the user enters YOLO by typing
   `/mode yolo` in the interactive CLI; a model tool cannot activate it.
2. Registered actions, including shell and file mutations, are preapproved
   within scope by explicit user activation. Do not seek routine approval.
   Tool-call, model-round, and verification-recovery caps are lifted for this
   session only; keep working until the task is done. After a workspace
   mutation, run an admitted verifier (test, lint/type check, verify script, or
   git_diff). Shell mutations still need that explicit verifier. Child agents
   do not inherit those ceilings.
   Explicit forced reviews and handoff-required actions still require review.
   Ordinary read-only observations (`read_file`, `list_directory`, `search_files`,
   and read-only slash aliases) may target sibling projects or other folders
   outside the current workspace. File mutations remain workspace-scoped.
   Curated policy, sensitive-path denials, safe mode, configured memory safeguards, and completion
   verification continue to apply. A wildcard tool set grants no authority.
   Intelligence is a runtime surface (`/intel status`, `/intel query TERM`).
   Promoted kernels are listed with `/kernel list`.
3. Keep credential operations private and email limited to drafts. Never route
   around a denial through shell, another tool, or a different mode.
4. Expect activation to end on workspace change, mode exit, or session end.
   `/reload` keeps this-process YOLO; it does not persist YOLO to disk.
   Saved configs and child agents cannot inherit activation or its grants.
5. Verify the active import and distribution before expecting source edits to
   affect a live command. A non-editable release install requires qualification
   and installation; it does not reload another checkout automatically.

Source anchors: `session_mode.select_mode`, `session_mode.active_mode`,
`nathan_runtime._prepared_grant`, `nathan_runtime.ask_approval`, and ALGO B51.
Boundary tests establish behavior, not a measured improvement in model quality.
When D-57 is selected, keep Echo disabled. Use /memory doctor to verify the
bridge and /remember or /memories for native write/recall routing; harness
refresh indexes skills but does not install code or select a memory backend.

For `action_program`, pass one `plan` JSON object:
`{"version": 1, "steps": [{"id": "v", "kind": "action", "action": "run_shell",
"args": {"command": "python -c \"assert True\""}}], "outputs": ["v"]}`.
`run_shell` `command` must be a string, not an object. Sibling `version`/`steps`
tool fields are accepted as a compatibility wrap of that object.
Omitted, null, or empty `outputs` automatically returns
the final step. Do not stop or seek expanded permissions for `outputs: []`.
Wrongly typed outputs or invalid step references are program-format errors,
not evidence of missing YOLO authority. A redundant action `cwd` matching the
active workspace is normalized away.
Omit `cwd`; changing it cannot redirect the program to another workspace.
Correct format errors in the same plan within existing authority and continue
instead of reporting that noninteractive approval is unavailable. Nested actions retain normal policy
and preapproval; version 1 still requires any mutating/code action to be final
and directly returned. Run successive mutations as ordinary tool calls rather
than assuming the program compiler can batch unsupported effect sequences.

A write to an existing file without overwrite is a typed denial, never an
unknown outcome and never a retry barrier. Read the file, then call write_file
with overwrite=true. Guardrail blocks (read-before-edit, already-exists) name
only that guardrail; they are not missing YOLO confirmation. A program that
reports status denied or failed keeps that status at the outer dispatch; do
not append or infer "Unknown outcome" or "approval is unavailable". Genuine
unknown mutations stay blocked until a fresh read or listing of the affected
workspace path reconciles them; then the same action may run again.
