# Ingest Algo CLI Rebrand Memory

This folder contains a compatibility helper for existing pre-rebrand installations that want to import the June 2026 Algo CLI facts into local memory.

## 1. Ingest Memory Facts (Recommended)

**Script:** `ingest-algo-cli-memory.ps1`

This PowerShell script safely adds the rebrand facts to your `~/.algo_cli/memory.json`.

### Usage

```powershell
# Dry run first (recommended)
.\scripts\ingest-algo-cli-memory.ps1 -DryRun

# Actually ingest the facts
.\scripts\ingest-algo-cli-memory.ps1
```

The script will:
- Read facts from `docs/memory-facts-algo-cli.md`
- Create a backup of your existing memory (`memory.json.backup-before-rebrand-ingest`)
- Skip any facts you already have
- Append only new facts

After running, you can verify inside Algo CLI with:
```
/memories
```

## 2. Alternative: Manual Ingestion via `/remember`

If you prefer to go through the official tool path (so the agent "experiences" the remembering), use the facts from:

- `docs/memory-facts-algo-cli.md`

Inside Algo CLI, run:
```
/remember <paste one fact at a time>
```

## 3. Lessons and reference content

For the local identity layer and optional reference pages:

- `docs/lessons-rebrand-algo-cli.md` → Append to `~/.algo_cli/identity/lessons-learned.md`
- `docs/quick-facts-algo-cli.md` → Good for dashboards or quick reference pages

## Files Overview

| File | Purpose | Target Location |
|------|---------|-----------------|
| `memory-facts-algo-cli.md` | Atomic facts for memory.json | Use with the ingest script |
| `lessons-rebrand-algo-cli.md` | Full lessons-learned entry | `~/.algo_cli/identity/lessons-learned.md` |
| `quick-facts-algo-cli.md` | Compact reference card | Wiki pages or memory overviews |
| `ingest-algo-cli-memory.ps1` | Automated memory ingestion | Run from this repo |

## End-to-End Flow Recommendation

1. Run the ingest script (dry-run first).
2. Append the lessons entry to your `lessons-learned.md`.
3. Run `/harness refresh` inside Algo CLI if you also changed indexed skills or reference files.

New installations do not need this migration helper.
