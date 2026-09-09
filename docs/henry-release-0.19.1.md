# 0.19.1 Release Scope

State: frozen stability candidate, not published. The published upgrade
baseline remains 0.18.0. This candidate inherits the reliability, security,
model-discovery, Windows, and delivery-path scope recorded in
[the 0.19.0 history](henry-release-0.19.0.md), with one additional browser fix.
The unfinished contained test runner and all prior scope exclusions remain out.

On 2026-09-09 the owner authorized fixing the browser race and qualifying a new
v0.19.1 tag, leaving v0.19.0 untouched and unpublished. The earlier environment
approval delegation was explicitly limited to 0.19.0; it does not silently
extend to this candidate. Publication and any renewed delegation require
explicit authority. No approval, test, or artifact check may be bypassed.

## Retained Failure

Protected-main CI 34401850330 at 70b2195b8f6844b987016cee2df359218c9edfed
passed platform, install/upgrade, quality, and build checks, but failed the live
browser gate with hosted_browser_entry_navigation_machine_terminal. No release
workflow was dispatched. The original tag v0.19.0 remains at
088d7753852a9e237979a76c254626c7482ea8ae and draft 385795143 remains unpublished.
Its earlier passing CI does not disprove the subsequently reproduced defect.

The real navigation machine succeeds when a terminal completion is the last
event in a receive batch, but the runner erroneously feeds a subsequent event
to the terminal machine when Chrome coalesces them. The same wrapper exists in
v0.19.0. The repair ends the inner receive loop at the first terminal result,
matching the existing behavior across separate reads. Denials, handoffs,
crashes, bounded decoding, and cleanup remain enforced. No automatic retry is
added, and a failed session cannot produce passing evidence.

## Qualification Required

- Focused regression tests and the full local suite.
- Exact protected-main CI on Windows, macOS, and Linux, including clean installs
  and all published-0.18.0 upgrade paths with synthetic state preservation.
- Five fresh hosted browser sessions, independent evidence validation, and
  source-bound attestation. Retain all rejected-origin and cleanup records.
- Reproducible distribution builds bound to the exact new tag, followed by all
  protected publication, durable-asset, provenance, and PyPI checks.

No 0.19.0 package bytes or browser qualification may be relabeled as 0.19.1.
The protected release workflow rejects the retired v0.19.0 input before checkout.

## Authorized Fixed-Tag Workflow Repair

On 2026-09-09 the owner explicitly renewed normal delegated environment
approvals and authorized publication only after every required gate passes.
That delegation was recorded in PR 48 comment 5609055435 and is limited to
0.19.1, without administrator bypass or independent-review claims.

Source 57a4740ab73a79244413a64396ee9e9f2285b738 passed all 16 protected-main
jobs in CI 34407105830, including five browser sessions, separate validation
and attestation, all 12 upgrade paths, and three native latency checks.
Tag v0.19.1 and draft 385866827 were then created at that exact source.
Publishing run 34408440047 failed closed in environment authority before
uploading any assets. The failed run remains failed and the draft remains empty.

The preflight queried repository variables with an actions-read job token;
that endpoint requires Variables-read authority unavailable in that job.
The repair reads the exact readiness flag from GitHub's workflow vars context,
like the browser gate, while retaining fresh environment-policy validation.
No timestamp or JSON API response is fabricated. Missing or non-true readiness,
missing credentials, HTTP rejection, and policy drift remain blocking.
Closed HTTP-status diagnostics distinguish transport failures without exposing
response bodies, tokens, or arbitrary exception text.

The owner separately authorized repairing this workflow and publishing the
unchanged qualified v0.19.1 tag from corrected protected main. Initial ancestor
draft recovery is restricted to that exact tag, source SHA, and draft ID.
Both package-source and corrected-publisher CI must pass; ancestry and the
real publisher certificate are verified independently of the unchanged package
bytes. The previous v0.19.0 recovery exception is replaced, not broadened.
The retired v0.19.0 tag and draft remain untouched and unpublished.
