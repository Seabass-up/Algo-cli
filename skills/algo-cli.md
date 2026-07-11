# Algo CLI — Local-First Agentic Terminal Assistant

**Type:** Primary Coding Agent / Tool  
**Command:** `algo-cli`  
**Current Version:** 0.14.0

## Overview

Algo CLI is a powerful local-first terminal agent designed for software engineering workflows. It excels at tool use, long-running tasks, context management, and integrating with a user's broader agent ecosystem via the harness RAG layer.

It was previously known as **ollama-cli** and underwent a complete rebrand in June 2026.

## Core Strengths

- Excellent structured tool calling with approval system and safe mode
- Strong identity layer (SOUL, IDENTITY, USER, lessons)
- Automatic skill crystallization from successful runs
- Sophisticated context handling (pruning, compaction, summarization); June 2026 hot-path pass: single system-prompt build per agent_loop iteration, O(n) tool pruning, shared embed unpack (`docs/algo-cli-hot-path-perf-2026-06.md`)
- Deep harness RAG integration across multiple agent tools
- Support for local Ollama + Ollama Cloud + optional xAI Grok OAuth (user-provided `XAI_CLIENT_ID`)

## Key Commands & Features

- `/memory-auto status|on|off`, `/remember`, `/memories`, `/forget` — bounded automatic and explicit long-term memory
- `/lesson` — append to lessons-learned
- `/agent` — structured multi-block agent pipelines
- `/harness`, `/hsearch`, `/hread` — harness RAG control
- `--oneshot --json` — machine-readable NDJSON output mode
- Full support for thinking models and reflection checkpoints

## Configuration

- Primary config directory: `~/.algo_cli/`
- Legacy location (`~/.ollama_cli`) is automatically migrated on first run
- Environment variables use `ALGO_CLI_*` prefix (legacy `OLLAMA_CLI_*` still supported)

## Usage Recommendations

Use Algo CLI when you need:
- Reliable long-horizon tool use with human oversight
- Strong memory and identity continuity
- Integration with other agent tools via harness
- High-quality local model performance with good context management

## Rebrand Notes

The project completed a three-phase rebrand in June 2026:
- Phase 1: Package and command rename
- Phase 2: Default directory migration to `~/.algo_cli`
- Phase 3: Full visible branding update

Legacy `ollama-cli` command and environment variables remain functional during the transition period.

## Related

- GitHub: https://github.com/Seabass-up/algo-cli
- Primary documentation: README.md in the source repository

**Last Updated:** 2026-07
