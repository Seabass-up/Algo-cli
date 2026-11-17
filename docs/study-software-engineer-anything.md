# Native Study Software Engineer Anything (SSEA)

SSEA is the Algo-facing name for MIT-licensed `rea-agents`
(https://github.com/morluto/rea). Algo exposes native registered tools; it does
not rename, install, update or download upstream engines.

## Supported native tools

- `ssea_status`: bounded metadata discovery, not a readiness/execution claim.
- `ssea_catalog`: exact supported operations, numeric options and limits.
- `ssea_analyze(operation, target, options?)`: one approved static inspection.
- `ssea_investigate(target, steps)`: up to four ordered inspections of one
  identical selected snapshot; stops on the first failure.

The qualified subset consists of actual upstream operations, not the earlier
proposed vocabulary:

| Operation | Supported input | Typed options |
| --- | --- | --- |
| `inspect_python_pyc` | CPython 3.7–3.13 pyc | `max_file_bytes`, `max_objects`, `max_depth`, `max_string_bytes` |
| `inspect_elf_notes` | ELF32/64 PT_NOTE/SHT_NOTE tables | `max_file_bytes`, `max_entries` |
| `inspect_pe_imports` | PE import/delay-load declarations | `max_file_bytes`, `max_dlls`, `max_functions` |
| `inspect_native_exports` | PE/ELF export declarations | `max_file_bytes` |

Use `ssea_catalog` for exact ranges/defaults. Inputs are regular single-link
files of at most 16 MiB, never directories. Upstream output per step is bounded
to 64 KiB; overflow is a reported unknown outcome, not silently clipped success.
The model-facing JSON has a separate smaller budget. Oversize detail is omitted
explicitly; workflow and step outcomes, binding digests and bounded evidence IDs
remain available. A stopped workflow cannot become successful because of size.
Each process has a 30-second deadline within a 60-second workflow budget.
Node V8 old-space is limited to 256 MiB; this is **not a total RSS limit**.

Example agent request:

> Use SSEA to inspect ELF notes and native exports in this explicitly authorized
> file: /absolute/path/to/library.so. Request approval for the exact workflow.

Both execution tools remain high-risk code-execution actions requiring action-time
approval. One-shot `--approval-mode auto` is not exact-action approval. No model
argument accepts a shell command, arbitrary argv, arbitrary MCP name, alternate
Node path or installation configuration. Ordinary prompts do not load this
specialist schema surface; `action_search` can discover it.
Relative targets are bound to the session workspace before approval, including
after `/cd`; they are not relative to the directory that launched the CLI.

## Execution boundary

This implementation is qualified only for macOS with the reviewed local
REA 6.8.0 tree and Node 24.22.2. Default installation identities:

- `~/.local/share/rea/lib/node_modules/rea-agents`
- `~/.local/share/rea/node-v24.22.2-darwin-arm64/bin/node`

Host-owned `ssea_install_root` and `ssea_node_path` can relocate the **same pinned
bytes**, not authorize different software. The packaged `resources/ssea-runtime.json`
binds Node plus a sorted SHA-256 tree of upstream JS/JSON/native-module files.
Drift, unsupported hosts or absent `sandbox-exec` fail closed; there is no
uncontained fallback. Updating REA/Node requires an explicit reviewed requalification.

The parent validates protected memory roots, symlinks, hardlinks and ancestor
identities. Descriptor-relative nofollow opens prevent link-replacement races
for selected inputs and runtime reads. A stable bounded input copy is created
inside private scratch and bound to the original by SHA-256. Original target
paths are never passed to REA. Snapshot mutation is denied in the child policy.

The OS Seatbelt profile starts with `deny default`. Reads are limited to the
reviewed runtime, minimal system libraries, explicit ancestor metadata and
scratch. It never allows `/System` wholesale (which includes a Data/home alias).
Protected roots are explicit deny rules even beneath allowed subtrees. Writes
are restricted to scratch. Network, Unix-service access, Mach lookup, process
fork and arbitrary executable launches have no grant. Node addons are disabled;
no target is executed. Environment and descriptors are not inherited, except
captured standard streams. Cancellation, timeout and output overflow terminate
the isolated process group and reap it. Scratch is removed on exit. Cleanup
failure is an explicit unknown outcome; it is never automatically retried.

The boundary does not protect against a hostile process running as the same OS
account changing trusted installation/configuration concurrently. Integrity
preflight is not a filesystem lock. It grants no new memory-store access or
external-data-sharing authority.

## Evidence and limitations

Results retain real upstream Evidence IDs, artifact digest, observations,
coverage and limitations. They are marked untrusted provider content. The adapter
checks operation/artifact identity but does not independently verify parser
findings or upgrade static declarations into runtime observations.

The old proposed `inspect_format`, `extract_strings`, `disassemble`,
`analyze_structure` and `trace_data_flow` names are unsupported, not approximate
translations. Ghidra/IDA/Hopper startup, JavaScript application workers,
disassembly, debuggers, target execution, UI control, traffic capture, downloads,
network and the rest of REA's catalog are **not enabled** by this static subset.
They need separate exact mappings and containment qualification.

## Qualification and delivery evidence

The native adapter is developed in the isolated source worktree
`~/Code/Algo-cli-ssea-runtime-20261117`, based on the active qualified 0.20.4 core
at local commit `12e54af`. Existing CLI-Anything, QC fixes and private speech
implementations are retained, rather than copying the older dirty 0.20.0 tree.
The original `~/Code/algo-cli` checkout remains unchanged by this runtime phase.

Tests include real REA executions on harmless pyc/ELF/PE fixtures, a real two-step
workflow, error-stop behavior, OS denial probes (unselected/protected/symlink
reads, outside writes, TCP/Unix sockets, child creation), environment isolation,
input alias/race rejection, timeout, output overflow and cancellation. Mocks are
not counted as upstream execution. Actual console-entry smoke uses a scripted
loopback conversational provider plus the real exact-action approval channel and
real sandboxed REA; it is not a live LLM-provider test.

Dated private build, regression, coverage, source/installed parity, preservation,
activation and normal-launcher smoke receipts are under:

`~/.local/share/algo-cli/qualification/ssea-20261117/`

The installed runtime's `LOCAL-RUNTIME.json` is the delivery authority for whether
that candidate was activated. Source presence, version equality and this document
alone do not establish activation. Existing interactive sessions must be
restarted to load new Python code. No public publication or model/speech-service
restart is part of this installation.
