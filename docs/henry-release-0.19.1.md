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
