# 0.19.0 Release Scope

State: frozen stability candidate, not published. The candidate version is
`0.19.0`; the published upgrade baseline is `0.18.0`. Do not reuse a published
version for changed development builds. After publication, reopen feature
development under the next `.dev1` version instead of changing released bytes.

## Source Boundary

The reviewed base is `5521eb3c83c1270c0e8f913e88fab07f06799700`, the head of
the successful native, runtime, distribution, and installed-wheel CI run
`34286935833`. That result is baseline evidence, not qualification of the new
release commit. Only release metadata, update diagnostics, delivery tests,
publication-policy corrections, and fixes to newly reproduced release blockers
may be added to this candidate.

Included work: governed Echo Veil 0.8.0 integration at the unchanged exact pin;
credential/startup and protected-path repairs; Responses stream and bounded
recovery fixes; Astra/model discovery; evidence-backed completion and pattern
runtime corrections; Windows security API initialization and path/newline
portability; locked Python/native/website dependency repairs.

Excluded work: contained verifier foundation commit
`69c1f3f4274d88e2d22873eecda429b82b5c1d62`, model-callable contained test
execution, new runtime profiles, browser/computer-control activation, signing
or notarization claims, production Echo qualification, and universal harness
performance claims. The candidate wheel smoke rejects both prototype modules.
Website source fixes are included in Git history, but website deployment is a
separate operation. Required browser qualification is not optional merely
because native control functionality remains disabled.

## Qualification Checklist

- Complete: select the frozen base without the contained verifier commit.
- Complete: distinguish candidate metadata from published 0.18.0 and update
  release notes and unchanged-update diagnostics.
- In progress: qualify the final candidate source and wheel, not an ancestor,
  on Linux, Windows, and macOS. Retain full tests, coverage, lint, typing,
  compilation, privacy/history and dependency scans, reproducible builds,
  artifact inspection, and native installed-wheel checks.
- In progress: exercise the updater actually shipped in the digest-pinned
  public 0.18.0 wheel. An isolated local wheelhouse supplies candidate bytes
  before publication; this is not a claim that PyPI serves the candidate.
  Test pip, uv, pipx with pip, and pipx with uv explicitly; automatic backend
  selection must not make an offline test silently resolve from public PyPI.
  Require fresh-process version/import identity, a repeated no-change update,
  and byte/permission/mtime preservation of synthetic configuration,
  credential files, saved memory, SQLite state, conversations, legacy state,
  and workspace edits. No real secrets or operator configuration may be used.
- Pending: protected-main browser qualification at the exact release source,
  owner approval, retained attestation, and package-bound evidence.
- In progress: verified publication configuration and PyPI authority inventory.
- Pending: explicit publication authorization. No release, tag, or publish
  dispatch is authorized merely by this preparation checklist.

The filesystem upgrade test does not qualify OS Keychain or Echo key migration.
Those authorities remain subject to their separate contracts. A green feature
branch with skipped protected-main jobs is not a fully qualified release.

Pre-commit macOS validation passed 5,421 tests with 35 platform/optional skips
and 69.37% branch-inclusive coverage against the unchanged 57% floor. Ruff,
284-module runtime typing, compilation, public-source scanning, the locked
dependency audit, and the exact Echo dependency audit passed. O4-O8 pattern
tests and the 160-cell lexical comparison were refreshed. M8 still has nine
passing local metrics and five blocked external metrics; M9 remains blocked.
These results do not substitute for the final commit's hosted platform jobs.

## Single-Owner Publication Policy

The owner explicitly approved using `Seabass-up`, immutable GitHub account ID
`184999458`, as the only release dispatcher, rerun actor, and required User
reviewer. Self-review and administrator bypass of the environment approval wait
are explicitly allowed. Teams, additional reviewers, other dispatch/rerun
actors, and unprotected source branches are rejected. A repository administrator
may skip the approval wait; this deliberately does not claim mandatory or
independent human approval. The agent must never approve or bypass a pending
environment job on the owner's behalf. Tag rules still have no bypass actors.

Keep immutable releases, exact no-bypass `refs/tags/v*` update/deletion
protection, protected-main source binding, short-lived scoped policy-audit
credentials, PyPI Trusted Publishing, and all artifact/evidence checks.
Readiness markers may be set only after the corresponding live controls have
been read back and verified. Missing external authority is a blocker, not a
reason to disable a check.
