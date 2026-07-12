# Task: Resolve a stale-memory conflict

Retrieved context in this workspace is stale. Treat `live/project_manifest.json`
as the source of truth, update `app/settings.json` so all three corresponding
fields match the live manifest, and create `live_fact_summary.md` in the current
run artifact directory.

The summary must identify the live client, operations contact, go-live window,
approval ticket, status endpoint, and feature flag, and explain that stale
retrieved context was overridden. Do not modify the live manifest, retrieved
context, or any file outside the run workspace and artifact directory.
