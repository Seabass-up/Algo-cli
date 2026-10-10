# Clean-History Candidate

Date: 2026-10-09. Local preparation and qualification only; not a release.

## Scope And Preservation

The operator approved a separate independent repository and local candidate
commits. Original source was preserved during that preparation. The subsequent
authorized delivery repair applies qualified Go/website changes locally;
original history, unrelated edits, normal launcher and saved configuration remain
preserved. The latest instruction is local CI only. No push, tag, publication,
remote configuration or history replacement was performed.

The source export root is
`e4334eaab3f7f8bbc4f134b5b9b14f96ab40d4a6`, on
`candidate/jev-runtime-reliability`. It contains 821 reviewed tracked working
files and nonignored product additions, not the original `.git`, environments,
caches, credentials, personal catalog or custom kernel implementations.
All exported file bytes and executable flags matched the original working
snapshot before candidate-only documentation was added. Original contents,
dirty status, HEAD and refs were independently reread unchanged.

Export snapshot SHA-256:
`0704febf63fcb8a1ba18ffba8f909f4a280bc8fcf63a667012360dd92ecd6ae1`.
Installed Python source SHA-256:
`5fa20aeb9ea6ad5455946865ba93c4c006f49e0ca6dd9e2a6ebea4761a76c854`.
Metadata remains 0.20.2; this candidate is not a new published version.

## Executed Qualification

The frozen noneditable environment uses Python 3.11.15 and the repository's
locked dev/supply-chain extras, with copy linking and exact installed-source
parity (271 Python modules). Product files did not change during qualification.

| Check | Result |
| --- | --- |
| Full Python suite, latest installed entrypoint | 7,382 passed, 42 skipped, zero failures/errors; 212.663 seconds |
| Coverage, latest run | 70.92% combined; line 74.17%, branch 61.67%; required floor 57% passed |
| macOS canonical/private-path alias | One layout-dependent skip passed separately under `/var/tmp` |
| Ruff / all-package mypy / compile | Pass; mypy covers all 271 modules |
| Source / reachable-history / wheel and sdist privacy | Pass with unchanged detectors; history uses CI's existing contributor-identity policy |
| Version / locked Python dependency audit | 0.20.2 / no known vulnerabilities |
| Nathan qualification / stored M9 audit | Current local runtime report passes; M9 is verified honestly blocked |
| Rust | Formatting, clippy with warnings denied, seven tests and advisory audit pass |
| Go 1.26.6 (before repair) | Race/vet pass; statement coverage 68.2%; vulnerability gate fails |
| Go 1.26.9 (applied candidate pins) | Default module-local selection reports go1.26.9 / auto; audit finds no vulnerabilities; uncached race/vet pass with 68.2% coverage and unchanged 60% floor |
| Website | Node 22 clean locked installation, build, 20 tests, lint and deployment dry run pass; full audit reports zero vulnerabilities |

The first candidate suite used an external environment. The hardened no-site
bootstrap correctly rejected the missing checkout-local `.venv`. That attempt
was stopped and retained; it is not a passing receipt. Creating a real locked
`.venv`, not a symlink or weakened check, made the exact guarded test pass.
The final complete run uses that environment and a dedicated pytest base path.
The macOS alias test was rerun separately because that base path is not below
the platform's `/var` alias. No existing temporary trees were removed to recover
the qualification.

Local receipt names: `full-suite-final.xml`, `coverage-final.xml`,
`macos-alias.xml`, `go-audit-old.json`, `go-audit-patched.log`,
`website-audit.json` and `website-tests.log`. Raw machine-specific logs stay
outside public source. Prior actual-agent Astra/Jev and injected-fault evidence
is bound to the same Python source digest; it was not rerun or re-labelled as a
new live provider demonstration in this candidate preparation.

## Go Pin Repair, 2026-10-09

Explicit candidate-only repair permission resolved the pending scope question.
Both `harness-gateway/go.mod` and `.github/workflows/oliver-ci.yml` now select
1.26.9, and the regression assertions require both pins. The language directive,
audit steps, race checks, coverage floor and execution permissions are unchanged.
From the gateway directory, `go version` reports `go1.26.9 darwin/arm64` and
`go env GOVERSION GOTOOLCHAIN` reports `go1.26.9` / `auto`; no forced-version
environment override or global Go setting was used.

Actual checks: pinned govulncheck v1.6.0 `./...` reports no vulnerabilities;
`go test -race -count=1 -coverprofile=... -covermode=atomic ./...` passes with
68.2% coverage; the unchanged 60% coverage check and `go vet ./...` exit zero.
The old nine symbol-level advisory findings included the upstream HTTP/2 race
[GO-2026-6617](https://pkg.go.dev/vuln/GO-2026-6617), fixed in 1.26.9. They were
an audit blocker, not an independently demonstrated application exploit.

One public synthetic Jev 1.13.0 check took 238.305 ms. It rejected the claim
that a forced-version run alone proves applied pins (0.98 confidence); its
patch-scope judgment was low-confidence (0.33) and was not accepted as proof.
Exact source diff, toolchain readback and executed gates establish this repair.

Post-repair policy/M9 and current Nathan/M8 artifact checks: 82 passed, zero
failures/errors/skips (`policy-final.xml`). The initial check correctly rejected
the stale M8 source digest. Its actual runner was rerun after the pin edits;
nine local metrics pass, five external metrics remain blocked, zero metrics fail.
New evidence rows were appended without rewriting prior rows or requirement
statuses; the M9 report was regenerated with `--expect-blocked`. Source,
reachable-history and rebuilt wheel/sdist privacy checks and installed Python
parity pass. Ruff and diff whitespace checks pass. The full 7,382-pass Python
run originally recorded 395.463 seconds and 73.22% coverage. The newer complete
installed `.venv/bin/pytest` run is separately reported in the table above;
neither receipt is relabelled as a hosted qualification.

## Delivery Repair Follow-up

Both the independent candidate and original active-launch checkout now use Go
1.26.9. Original module-local audit/race/vet pass, coverage remains 68.2%, and
38 original policy tests pass using the isolated candidate's dev environment;
the normal production environment was not populated with testing dependencies.
Three original current-artifact checks pass. Candidate/original M8 source hashes
were exactly equal, so the exact actual-run receipt was reused with explicit
ledger provenance, not called a new original-checkout run or rewritten digest.

Website Next/eslint-config-next are patched to 16.3.8, Cloudflare tooling to
vite-plugin 1.63.1 / Wrangler 4.149.0, and sharp to 0.35.5 with librsvg 2.63.2.
Compatible transitive patches clear undici, source-map-js, brace-expansion and
fast-uri advisories. One remaining unpatched braces advisory initially propagated
to eight packages. Replace its Fast Glob engine for the two pinned synchronous
consumers using exact Glob 13.0.6 and patched brace-expansion 5.0.12, without
removing Next rules or Vinext CommonJS transformations. The initial Tinyglobby
attempt was rejected after review reproduced an unmatched-class budget bypass,
changed dot-dot switch keys, missing wildcard symlink roots and incorrect numeric
brace ranges. Executable regressions cover those four failures, actual lint
coverage and transformed-module execution. The final limited adapter is not a
general Fast Glob replacement. It preserves directory, absolute, symlink and
lexical prefix identity and rejects pathological nesting and excess expansion
before discovery rather than truncating away lint roots. The final
lock and installed graph contain neither braces nor micromatch; clean `npm ci`,
full `npm audit`, 20 tests, lint and Cloudflare dry-run packaging pass. No audit
suppression, framework downgrade, production deploy or broad install-script
permission was used. Final local qualification uses Node 22.23.3 / npm 10.9.9,
downloaded from the official distribution and SHA-256 verified, matching CI's
Node 22 family. Earlier exploration used Node 26.4.0 / npm 12.0.2. Hosted runners
and native Windows execution remain unqualified.

Three macOS real-updater fixtures pass from the public 0.20.1 wheel to the
qualified local 0.20.2 wheel: pip, uv-tool and pip-less uv-pip. Each verifies both
installed payloads, preservation of all 11 synthetic state files including
configuration/credential/memory/workspace and SQLite integrity, and repeat-update
idempotence. No real credentials were used; OS Keychain was not qualified. This
does not mean public PyPI can install 0.20.2. These fixtures bind the earlier
Go-qualified CLI wheel, whose Python payload is unchanged by website-only edits.
Current full local Python, all-package mypy/Ruff/parity, seven Rust tests,
clippy/format/audit and Go gates pass. The replacement publication plan is in
`clean-history-publication-plan.md`; it is not executed under local-only CI.
The resumed final run uses `python-complete.xml` and `coverage.xml`; website
and original propagation checks use `website-test.log`, `original-test.log`,
`website-audit.json` and `original-audit.json`. These are local receipts, not
deployed or hosted artifacts. Both website roots pass the same 20 tests, audit,
lint and clean install on Node 22.23.3/npm 10.9.9.

## Remaining Gates

- Website dependency repair and local Node 22 behavior are verified; deployed
  website bytes and hosted platform behavior are not qualified by the local run. Website
  source remains excluded from CLI distributions.
- M8 external browser/signing qualifications and M9 release gates remain blocked;
  public-claim eligibility remains false. Hosted platform qualification and
  public upgrade preservation were not performed here.
- Original history still has five privacy findings. Only the independent
  candidate's reachable history is clean. Merging/fetching the old refs into it
  would defeat that boundary. Publication/history replacement needs new explicit
  authority and an appropriate repository/policy plan.

## Reproduction

From this independent checkout:

```sh
uv sync --frozen --no-editable --extra dev --extra supply-chain --reinstall-package algo-cli-runtime --link-mode copy --python 3.11
.venv/bin/python -I scripts/oliver_installed_source_parity.py
.venv/bin/python scripts/check_public_release.py
.venv/bin/python scripts/check_public_history.py --allow-contributor-identities
ALGO_TEST_REQUIRE_RIPGREP=1 .venv/bin/python -m pytest tests --cov --cov-branch --cov-fail-under=57
```

Choose a new owned pytest base directory for each qualification; the `/var` alias
regression needs a canonical path below `/private/var`. Never reuse another run's
directory or clear unrelated caches/locks to make a test pass.

One bounded public-synthetic Jev 1.13.0 claims check took 280.101 ms. It supported
the independent candidate and rejected history-only release readiness. Its
shared-worktree judgment was uncertain; actual Git and scanner behavior, not a
model vote, establishes preservation and reachable-history results.
The delivery-repair claims check took 284.688 ms and supported both preserving
behavior over forced downgrades and keeping audit proof distinct from rendering
proof (0.98 confidence each). Executed tests and resolved graph remain authoritative.
