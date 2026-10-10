# Website Dependency Repair

Date: 2026-10-09. Local qualification and focused commits only.

## Symptom And Confirmed Causes

The full website audit reported 17 findings: one critical, 15 high and one
moderate. Patched compatible versions cleared the Next/image-decoder/network/
parser advisories. Eight inherited findings remained from unpatched `braces`
through Next lint and Vinext's dynamic CommonJS discovery.

An initial Tinyglobby replacement had a zero audit but failed independent
executable review. It allowed an unmatched character-class prefix to mask the
nesting budget, normalized away dot-dot switch keys, omitted wildcard symlink
roots and changed multi-digit numeric brace ranges. It was not accepted.

## Final Repair

- Next/eslint-config-next 16.3.8, Cloudflare vite-plugin 1.63.1, Wrangler 4.149.0,
  sharp 0.35.5 and compatible transitive patches.
- A local synchronous adapter backed by exact Glob 13.0.6 and patched
  brace-expansion 5.0.12, resolving at both affected callers. It is not a general
  Fast Glob replacement. It supports their `cwd`/`onlyDirectories` options and
  rejects unsupported options and mixed absolute/relative pattern arrays.
- Separate brace/parenthesis depth budgets, bounded expansion before discovery,
  no truncation of over-budget roots, and retained lexical dot-dot switch keys,
  numeric ranges and directory symlinks.
- Actual Next lint and Vinext transformation regressions, including executing
  a transformed dynamic require. Lint rules and CommonJS support remain active.
- Exact decoder-version checks plus working AVIF and SVG controls for both
  Next and Miniflare. No deployed exploit reproduction is claimed.

## Local Verification

The website checks use official Node 22.23.3/npm 10.9.9, matching CI's Node 22
family. The downloaded Node archive matches its official SHA-256 manifest.

```sh
npm ci
npm test
npm run lint
npm audit
npm run deploy:cloudflare:dry-run
```

Candidate results: clean install passed; build plus 20 tests passed with zero
failures/skips; lint passed; full audit reported zero vulnerabilities; deployment
dry run passed without uploading. Resolved graph no longer contains `braces` or
`micromatch`. It does contain the distinct, patched `brace-expansion` package.

The original checkout receives nine byte-identical verified website files, guarded
against concurrent edits. Its own installation/test/lint/audit passed: 20 tests,
zero failures/skips and zero advisories. Unrelated files, configuration, launcher and private history
are preserved. Test the isolated candidate environment rather than installing
test dependencies into the normal CLI environment.

The final frozen Python run records 7,382 passes, 42 skips, zero failures/errors
in 212.663 seconds and 70.92% combined coverage against the unchanged 57% floor.
Ruff, all 271-module mypy and installed-source parity pass; the Python digest is
unchanged. Locked Python audit, Rust format/clippy/seven tests/audit and Go
audit/race/vet pass, with 68.2% Go coverage against the unchanged 60% floor.
Earlier macOS pip/uv-tool/uv-pip predecessor-upgrade fixtures preserved all 11
seeded state files and repeat-update integrity; they are not a public new-version
upgrade or OS Keychain qualification.

## Prevention And Limits

Keep the dependency override and its vendor package together; test clean `npm ci`
from the lock, not only the current node_modules tree. Dependency upgrades must
exercise the actual caller semantics, not just version or model approval. Never
disable lint rules, omit dev dependencies, force a framework downgrade or bypass
transport/install-script protections merely to clear an audit.
Set repository-local `install-links=true` and an explicit package file list so
the adapter is packed under node_modules instead of generating dependencies in
vendor source. The source scanner correctly rejected that generated tree; those
owned outputs were moved to temporary staging, not whitelisted or committed.
The caller regression also asserts its installed implementation is outside source.

The earlier caller/packaging failures are retained as failures, not relabelled
green receipts. The source/index/privacy and installed-runtime checks remain
separate from website rendering. The normal Python payload is unchanged by this
website-only repair. No push, tag, hosted dispatch, public release or deployment
is authorized. Signed native/browser and hosted Windows/Linux qualification
remain external prerequisites; see `clean-history-publication-plan.md`.
