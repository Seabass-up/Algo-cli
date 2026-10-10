# Clean-History Publication Plan

Date: 2026-10-09. The owner authorized preparation of the core CLI 0.20.3,
reviewed source integration and hosted CI, while keeping native features disabled
and forbidding publication until every required core-release gate passes.
The earlier local-only instruction is superseded for this bounded integration.
Publication, environment approval, normal installation and website deployment
are separate actions, not consequences of a successful local test.

## Source And Privacy Boundary

Use the independent `candidate/jev-runtime-reliability` repository as the source,
not the original repository's historical refs. Its root is
`e4334eaab3f7f8bbc4f134b5b9b14f96ab40d4a6`; qualify the final repair commit,
not just that root or an earlier successful test run. Preserve the original
dirty working tree and history privately. Do not merge or fetch its old refs
into the sanitized candidate.

Keep the public algorithm catalog as a starter template. Preserve the generic
kernel framework, not the owner's catalog/custom implementations. Credentials,
memory databases, profile, workspace settings and local environments are not
release content. Scan all reachable candidate refs and the actual wheel/sdist
with the existing detectors, without weakening their policy.

## Gates Before Publication

1. Finish source/test/dependency edits; freeze a clean exact commit and explicit
   stable scope/version. Do not move or reuse a published immutable tag.
2. Run complete frozen installed-source Python, native helper, lint/type,
   supply-chain and privacy checks. Verify the website's complete development
   dependency graph and both synchronous glob consumers; retain lint rules and
   CommonJS transformation. A zero audit without clean-install and behavior
   tests is insufficient.
3. Qualify that exact source on macOS, Windows and Linux, including clean
   installation and real predecessor upgrades. Local macOS synthetic-state
   tests are supporting evidence, not hosted or public-new-package proof.
4. Complete the required hosted managed-browser qualification and attestation.
   The signed native-browser companion remains disabled and outside this core
   CLI release. Its missing identities/runners and blocked M8 metrics remain
   honest limitations, not passes. Do not substitute ad-hoc signing or expose
   unqualified native features.
5. Revalidate release/source bindings, protected environment/reviewer policies,
   publisher authority, artifacts and public registry state. Keep blocked M8/M9
   evidence and false public-claim eligibility until real evidence satisfies
   their contracts. Ordinary PR/hosted CI is authorized; publication still waits
   for the exact-source required gates and protected release authority.

## Controlled History Cutover

**Fresh public inventory:** A separate read-only mirror of the actual public
repository, including branches, tags and advertised PR refs, has 348 reachable
commits and passes the unchanged all-refs history scanner with contributor
identities allowed. Its default branch is
`6cac02a4b1507ee38f2dae147337aac3913c3b90`. The five findings in the original
local repository were therefore not reproduced in this public inventory.
This does not prove erasure of undiscoverable cached objects or forks.

Use a fresh integration checkout based on that clean public main and apply only
the reviewed candidate source. Do not import original local refs or merge the
independent candidate's unrelated root history. Review the exact delta and run
privacy/artifact/CI gates, then use the protected normal PR path. Preserve all
existing immutable tags. A force rewrite of an already-clean public main is
unnecessary and not the selected cutover mechanism.

If a later inventory actually finds public residue, inventory branches, tags,
releases, PR refs, forks and cached
objects containing private residue. Keep a private recoverable archive and
compare-and-swap expected remote heads. Decide whether a new clean public
repository or a coordinated replacement in the existing repository best
preserves issues, release identity, package publisher bindings and consumer URLs.
Do not push the private backup refs to a public repository.

Replacing `main` alone does not remove old tags, PR references, forks or cached
commit pages. Follow GitHub's sensitive-data removal process for affected
objects, involve repository owners/fork owners and GitHub Support where required,
and rotate any actually exposed credentials. Do not disclose raw scanner
findings in public issues. The plan does not claim remote erasure has occurred.
Reference: [GitHub sensitive-data removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

Only after every required gate passes and the cutover details are authorized:
publish through the protected release workflow, verify immutable source and
artifact identities, wait for exact public PyPI visibility, and rerun public
clean install/predecessor upgrade/repeat update with synthetic preservation
fixtures. Check configuration, credentials, memory, workspace bytes/modes/mtimes
and SQLite integrity. Never use the owner's real credentials in these fixtures.

## Observed Missing Prerequisites

The local signing identity check found zero valid identities. The read-only
GitHub inventory found no self-hosted signing runners and no `native-hardening`
environment. Browser/release environments and their readiness variables exist,
but presence is not successful qualification. The observed public GitHub/PyPI
stable version remains 0.20.1. These observations are dated, not future state.
No hosted Windows/Linux or public 0.20.3 upgrade claim is made by this plan.
