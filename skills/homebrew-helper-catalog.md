---
name: homebrew-helper-catalog
description: Curated optional Homebrew formulae and casks known to Algo CLI, with platform limits and safe selection guidance.
tags: [algo-cli, homebrew, extensions, cli, tui, macos, optional-tools]
created: 2026-08-13
---

# Homebrew Helper Catalog

## Trigger

Use this catalog when a task names one of the packages below, asks which optional
local tool could help, or asks whether a helper is installed. Call the read-only
`extensions_manifest_build` tool first; it implements the discoverable
`extensions.manifest` kernel capability. A catalog entry means **known**, not
installed, trusted, authenticated, or approved for execution.

## Safety contract

1. Never install, upgrade, uninstall, tap, launch, authenticate, or start a
   service merely because a package appears here.
2. Ask for approval before `brew install`, `brew install --cask`, app launch,
   shell-profile modification, package-managed auto-update, or network/account
   setup.
3. Prefer Algo's bounded native tools over an external helper when both can do
   the task. Use `run_shell` only for a concrete user task under its normal
   policy and approval gates; catalog membership grants no subprocess authority.
4. Treat databases, Kubernetes clusters, NATS, Sentry, Glean, finance services,
   and agent platforms as external systems. Read/write/network effects remain
   governed by their normal approvals.
5. Never expose `.env` values, credentials, tokens, connection strings, source
   code, medical data, or proprietary workplace content in prompts or logs.
6. Verify `status`, `path`, and `version` from `extensions.manifest`; do not infer
   installation from this document.

## Status semantics

- `ready`: a Homebrew receipt and expected command/app artifact are present.
- `installed`: the Homebrew receipt exists, but there is no direct callable
  artifact or its expected artifact was not found.
- `missing`: no Homebrew receipt was found.

The manifest probes paths and receipts only. It does not execute Homebrew or any
helper. Version and package metadata below were checked from local Homebrew
metadata on 2026-08-13 and will age; re-check before relying on versions.

## Formulae

| Formula | Expected interface | Best use in Algo tasks | Boundary / platform note |
|---|---|---|---|
| `awww` | `awww` | Wayland wallpaper automation | Linux/Wayland-oriented; not useful for native macOS wallpaper control. |
| `b4n` | `b4n` | Interactive Kubernetes API browsing | TUI; cluster access can expose or mutate resources depending on use. |
| `bluetuith` | `bluetuith` | Interactive Bluetooth management | TUI; platform support varies and Bluetooth changes are consequential. |
| `cliphist` | `cliphist` | Wayland clipboard history | Linux/Wayland-oriented; clipboard history may contain secrets. |
| `cuttlefish` | `cuttlefish` | Compacted de Bruijn graph construction | Specialist genomics workload; validate inputs and output storage. |
| `evnx` | `evnx` | `.env` management | Secret-bearing files: never print values or persist them in harness records. |
| `fluxcd` | `flux` | Flux CD/Kubernetes operations | Cluster/network effects; inspect context and require approval for mutations. |
| `fusesoc` | `fusesoc` | HDL package/build abstraction | Useful for reproducible FPGA/ASIC builds; pin core/library provenance. |
| `inshellisense` | `is` (`inshellisense` alias may exist) | Interactive shell completion | `is init`/`reinit` modifies shell setup; ask first. |
| `kata` | `kata` | Local-first federated issue tracking | Repository/local-state writes require normal mutation approval. |
| `lld@22` | keg-only (`ld.lld` under its opt prefix) | LLVM 22 linking | Versioned, keg-only; do not rewrite PATH/toolchains implicitly. |
| `llvm@22` | keg-only (`clang` under its opt prefix) | LLVM/Clang 22 compilation and language tooling | Versioned, keg-only; unversioned `llvm` may coexist and is not equivalent provenance. |
| `nats` | `nats` | NATS Server and JetStream administration | External network/admin actions can publish, delete, or reconfigure data. |
| `rammap` | `rammap` | Read alignment/mapping | Specialist genomics workload; preserve reference/read provenance. |
| `svlang` | `slang` | SystemVerilog compiler/language services | Formula and executable names differ; verify subcommands against the installed version before automation. |
| `systemd-lsp` | `systemd-lsp` | systemd unit language service | Primarily Linux/systemd content; useful on macOS only for editing remote/Linux repos. |
| `tracy-genomics` | `tracy` | Sanger chromatogram basecalling/alignment/assembly | Do not confuse with the unrelated Tracy profiler formula. |
| `vapoursynth-vszip` | VapourSynth plugin (no direct command) | Zig image-processing filters in VapourSynth | Manifest should normally show `installed`, not `ready`; use through VapourSynth/Python. |
| `vi-sql` | `vi-sql` | Interactive SQL database workbench/MCP | Connections and queries can expose or mutate databases; inspect target and mode first. |
| `xmedcon` | `medcon` (GUI may also be supplied) | Medical-image conversion | Medical data may be sensitive; preserve metadata and verify conversions independently. |

Installation syntax, only after explicit approval:

```bash
brew install PACKAGE
```

## Casks

| Cask | Artifact | Best use in Algo tasks | Boundary |
|---|---|---|---|
| `bb` | `bb.app` | GUI IDE/orchestrator for coding agents | GUI-only from Homebrew metadata; no native Algo action. |
| `ds4-control` | `DS4 Control.app` | Menu-bar pane for DeepSeek V4 | GUI/account/network boundary; do not treat it as a model provider automatically. |
| `font-jetendard` | fonts | Typography asset | Knowledge/design resource only; no executable capability. |
| `font-nexon-football-gothic` | fonts | Typography asset | Knowledge/design resource only; no executable capability. |
| `font-nexon-kart-gothic` | fonts | Typography asset | Knowledge/design resource only; no executable capability. |
| `glean` | `Glean.app` | Workplace search/AI assistant | Enterprise data and authentication boundary; never assume access or export content. |
| `grok-bot` | `Grok Bot.app` | Cross-app AI teammate | GUI/account/network boundary; distinct from Algo's xAI provider integration. |
| `mongrel` | `Mongrel.app` | Database/container/Kubernetes/API workbench | GUI with potentially destructive external-system access. |
| `muse-code` | `muse` | Interactive terminal coding agent | External agent; do not delegate secrets or grant unreviewed workspace writes. |
| `owlocr` | `OwlOCR.app` | On-device OCR for images/PDFs | GUI-only; prefer Algo's `read_pdf`/vision path unless OwlOCR materially helps. |
| `petdex` | `Petdex.app` | Visual coding-agent activity pet | UI/entertainment only; no harness execution value. |
| `sentry-cli` | `sentry-cli` | Sentry release/event administration | Network/auth and release mutation boundary; never capture tokens in chat or logs. |
| `sina-finance` | desktop app | Market data and financial news | Information source only; verify consequential financial claims independently. |
| `subtitle-edit` | `Subtitle Edit.app` | Subtitle authoring/conversion | GUI-only from Homebrew metadata; verify timing/encoding after conversion. |
| `warp-agent-cli` | `warp` | Terminal agentic development environment | External agent/runtime; Algo remains the active policy and approval boundary. |

Installation syntax, only after explicit approval:

```bash
brew install --cask CASK
```

## Selection guidance

- **Potentially useful CLI/TUI helpers:** `b4n`, `cuttlefish`, `evnx`, `fluxcd`,
  `fusesoc`, `inshellisense`, `kata`, `lld@22`, `llvm@22`, `nats`, `rammap`,
  `svlang`, `systemd-lsp`, `tracy-genomics`, `vi-sql`, `xmedcon`, `muse-code`,
  `sentry-cli`, `warp-agent-cli`.
- **Linux/Wayland-focused:** `awww`, `cliphist`; `bluetuith` is cross-platform
  but system integration still varies.
- **Plugin rather than CLI:** `vapoursynth-vszip`.
- **GUI-only or primarily GUI:** `bb`, `ds4-control`, `glean`, `grok-bot`,
  `mongrel`, `owlocr`, `petdex`, `sina-finance`, `subtitle-edit`.
- **Fonts only:** `font-jetendard`, `font-nexon-football-gothic`,
  `font-nexon-kart-gothic`.

Do not create native model-callable actions for these packages by default. A
future native adapter is justified only when it has bounded arguments,
structured output, explicit effect classification, time/output limits,
`subprocess(..., shell=False)`, and tests for missing binaries, injection-shaped
input, nonzero exits, and external side effects.
