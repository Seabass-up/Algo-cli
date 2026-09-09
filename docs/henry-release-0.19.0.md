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
  Windows 0.18.0 has a reproduced active-launcher WinError 32 failure. Qualify
  the external owning-manager command from that installation instead, and
  separately require the candidate launcher to refuse unsafe self-replacement.
  Windows receipts must not claim the published self-updater was exercised.
  Require fresh-process version/import identity, a repeated no-change update,
  and byte/permission/mtime preservation of synthetic configuration,
  credential files, saved memory, SQLite state, conversations, legacy state,
  and workspace edits. No real secrets or operator configuration may be used.
- Pending: protected-main browser qualification at the exact release source,
  owner approval, retained attestation, and package-bound evidence.
- Complete as of 2026-09-09: signed-in PyPI authority inventory. The only
  project collaborator is Owner `seabass-up` with 2FA; the account has no API
  tokens, no organizations, and no pending publishers. The only active trusted
  publisher is `Seabass-up/Algo-cli` + `oliver-release.yml` +
  `release-authority`. No authority was changed during this read-only audit.
  Recheck time-sensitive authority before publication; this is not evidence
  that the candidate has been published or its browser gate has passed.
- Authorized on 2026-09-09: qualify and publish 0.19.0 under the bounded owner
  delegation below. This does not establish readiness or authorize publication
  while any required qualification remains failed or missing.

The filesystem upgrade test does not qualify OS Keychain or Echo key migration.
Those authorities remain subject to their separate contracts. A green feature
branch with skipped protected-main jobs is not a fully qualified release.

Report-contract unit tests use a scoped synthetic stopwatch; their fixture
timings must never be treated as host performance evidence. Each native OS CI
job separately runs the real benchmark in a fresh process with the unchanged
latency limits, 101 contract/context samples, 31 checkpoint/workload samples,
and five warmups. Both passing and failing reports are retained by OS and run
attempt; a failed benchmark exits nonzero and blocks the platform job. The
diagnostic profiler cannot replace that result. This separates serialization
and tamper checks from performance qualification without removing either gate.

PR run 34376553975 observed Windows workload-total p95 of 7373.073 ms against
3500 ms and time-to-first-action p95 of 2808.8928 ms against 1000 ms. Its
20-sample, zero-warmup report fixture caused four test failures despite all
17 correctness probes passing. The subsequent five-sample diagnostic profile
is not replacement qualification. Retain this failure and require fresh
source-bound native measurements; do not raise the limits or relabel it green.

Candidate run 34303669767 passed source-bound packaging and all four Linux and
macOS upgrade paths, but Windows upgrade qualification found the real 0.18.0
launcher lock and a synthetic SQLite connection cleanup defect. Preserve that
failed run as diagnostic evidence; the corrected Windows path requires a new
exact-commit qualification, not a relabeling of the prior failure.

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
independent human approval. Agent approval on the owner's behalf is prohibited
except for the explicit, bounded delegation below. Tag rules still have no
bypass actors.

### Owner Delegation for 0.19.0

On 2026-09-09, after the manual-only restriction was explained, the owner
explicitly instructed Codex to use the signed-in GitHub account to perform the
reviews and complete publication so `algo-cli update` can obtain the release.
This supersedes the manual-only restriction solely for `browser-hardening` and
`release-authority` jobs needed to qualify and publish 0.19.0 from protected
`main`, including its corrective pull requests. Use the normal review UI as
`Seabass-up`, not administrator bypass, and record the delegation plus exact
run and source revision in each approval comment. Do not represent delegated
approval as independent or personally performed human review.

The delegation does not permit another identity, changed environment settings,
weaker checks, unqualified artifacts, native-signing substitutes, skipped
qualification, tag rewrites, or a later release. It expires when 0.19.0 is
published or the owner revokes it. Any remaining technical failure blocks
publication regardless of this authorization.

Keep immutable releases, exact no-bypass `refs/tags/v*` update/deletion
protection, protected-main source binding, short-lived scoped policy-audit
credentials, PyPI Trusted Publishing, and all artifact/evidence checks.
Readiness markers may be set only after the corresponding live controls have
been read back and verified. Missing external authority is a blocker, not a
reason to disable a check.
