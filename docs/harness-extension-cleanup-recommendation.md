# Structured Codex plugin metadata indexing

**Status:** Completed and verified.

## Current State

When the user explicitly enables external harness sources, Algo CLI indexes Codex plugin metadata without treating it as undifferentiated extension noise:

- `codex:extension` for `SKILL.md` files
- `codex:plugin` for `.codex-plugin/plugin.json` manifests
- `codex:install` for `.codex-remote-plugin-install.json` install receipts
- `codex:connector` for `.app.json` connector metadata
- `codex:mcp` for `.mcp.json` server declarations
- `codex:command` for plugin command markdown
- `codex:agent` for plugin agent profiles

JSON metadata is normalized into useful titles, descriptions, and tags. For example, Google Drive connector records surface as `Codex app connectors: google-drive` instead of raw `.app` filenames, and remote install receipts surface as `Codex plugin install: google-drive`.

## Why This Matters

The original audit found that extension records could dominate the harness index and dilute semantic retrieval. The current implementation keeps the useful plugin capability information while avoiding raw, low-context metadata records.

The live harness quality report distinguishes generic extension records from structured plugin metadata, so a larger Codex plugin cache no longer makes the harness look unhealthy by itself.

## Verification

Relevant tests:

- `tests/test_harness.py::test_iter_files_supports_nested_plugin_cache_globs`
- `tests/test_harness.py::test_codex_plugin_metadata_sources_are_builtin_harness_sources`
- `tests/test_harness.py::test_codex_plugin_manifest_record_uses_json_metadata`
- `tests/test_harness.py::test_codex_remote_plugin_install_record_uses_json_metadata`
- `tests/test_harness.py::test_codex_connector_and_mcp_records_use_json_metadata`
- `tests/test_harness.py::test_quality_allows_low_project_share_when_structured_plugin_metadata_is_indexed`

Operational checks:

- Run `/harness status` to inspect counts and quality.
- Run `/harness score` to grade readiness against the current Algo CLI review gates.
- Run `/harness refresh` after plugin/source changes.
- Run `/harness embed` if pending embeddings appear.
