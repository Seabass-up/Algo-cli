# M8 External Browser Qualification

Current investigation: 2026-09-04. This is a prerequisite receipt and remaining
work plan, not production qualification evidence.

## Completed prerequisites

- Started Docker Desktop; daemon reports Linux/aarch64, Docker 29.6.1.
- Existing live broker probe passed pinned-peer, upstream TLS, interception TLS,
  and HTTP mediation checks. It used a local socketpair, not a Chrome process.
- Existing Docker boundary probe passed with its pinned Linux/amd64 Python
  image under local emulation. Public IP, metadata and host-alias connections
  were blocked; non-root, read-only, capability, namespace and resource checks
  passed. Probe containers and networks were absent after cleanup. The arm64
  attempt returned `command_failed`; no native-arm64 qualification is claimed.
- Created and read back the live `browser-hardening` GitHub environment with
  sole reviewer `Seabass-up` (user ID `184999458`), self-approval permitted,
  protected branches only, and admin bypass disabled. The owner explicitly
  requested the single-maintainer policy. This is not independent review.
- Updated the local workflow to require that owner as both original actor and
  rerun initiator, and to reject any extra reviewer, team, or policy drift.
  Regression tests execute the actual embedded validator and shell gate.

## Activation still required

At the initial investigation, the workflow changes were local and the repository
readiness marker was unset. Land the reviewed changes on protected main,
read back the environment policy again, then set
`BORON_HARDENING_ENVIRONMENT_READY=true`. The owner must trigger the run and
manually approve its environment jobs. Do not use the marker as an M8 pass or
automatically approve a run on the owner's behalf.

GitHub supports this self-approval configuration through the
[deployment environment policy](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments).
The native-hardening and PyPI policies were not changed.

## Remaining engineering and qualification

1. Rebuild and qualify the managed-browser images on the hosted Linux/amd64
   path. The source still pins Chrome `151.0.7922.108`; the strict Google
   VersionHistory fetch observed stable `152.0.7977.82`. Refresh verified image
   materials and digests, not just the version string, and obtain a fresh
   release observation during each run. Enforce the existing 72-hour lag limit.
2. Implement the selected-Chrome native authority bridge and supported actions.
   `NeonNativeHostMain` currently exits with `protocol_disabled`; the extension
   only observes and hands off. Removing that guard alone is not an implementation.
3. Provision Developer ID signing and the installation/permission lifecycle.
   No valid code-signing identity was installed, and the owner confirmed no
   Developer ID Application certificate is available. Ad-hoc signing can test
   development builds but cannot satisfy the production signing contract.
4. Implement and run the actual supported-task matrix with independent
   outcome checkers, at least five cold/warm rotated repetitions, and matched
   semantic/screenshot baselines. Record profile/network isolation and current
   browser security evidence alongside the performance results.
5. Add a source-bound external qualification ingestion path with strict
   provenance and report validation. The current local M8 command emits five
   blocked external metrics unconditionally; a successful narrow Boron hosted
   GET does not satisfy those metrics. Only then update the evidence ledger and
   run the M9 completion audit against the retained authoritative artifacts.

Local broker and Docker probes do not establish task completion, selected-tab
actions, screenshot savings, native signing, or M8 release eligibility.
