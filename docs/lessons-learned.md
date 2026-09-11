# Lessons Learned

Record verified repairs here before closing the associated task. Update an
existing incident instead of duplicating it. Each entry needs an observed issue,
confirmed cause (or an explicit unknown), repair, verification, prevention, and
remaining limits. These are dated receipts, not proof of future runtime state.
The [changelog](../CHANGELOG.md) and [release history](henry-release-0.19.1.md)
retain the earlier reliability, model-discovery, Echo, Windows, and browser
qualification repairs; this log adds the verified release-closeout lessons.

## 2026-09-10: Public Release And Upgrade Recovery

**Issue:** Source development had advanced while `algo-cli update` still saw
published `0.18.0`. The immutable `0.19.1` candidate subsequently failed the
public-package boundary.

**Cause:** Repository progress is not an indexed release. The candidate's public
metadata also contained a direct Git dependency that PyPI would not accept.

**Repair:** Prepare and qualify the packaging-compatible `0.19.1.post1`
successor. Keep Echo Veil's exact source pin in the development/qualification
dependency group without emitting a direct URL in public dependency metadata.
Retain the old immutable tags rather than moving or repackaging them.

**Verification:** Source `09428d131cbd11fa76268cb17e394368dc3f6934` passed its
declared delivery gates. Protected publication run
[34474096803](https://github.com/Seabass-up/Algo-cli/actions/runs/34474096803)
completed, and live public GitHub/PyPI checks confirmed `0.19.1.post1`, 17 explicit
GitHub assets, non-yanked distributions, and matching sizes and SHA-256 digests.
The session's isolated public clean install and real `0.18.0` update preserved
11 synthetic state files' bytes, modes, and modification times, with SQLite
integrity and repeat-update checks passing. Exact release identities and hashes
are retained in [the release receipt](henry-release-0.19.1.md#publication-receipt---2026-09-10).

**Prevention:** Qualify package metadata before tagging; then test the actual
public updater and state preservation. Keep prepared, qualified, uploaded,
indexed, published, immutable, and upgradeable states separate.

## 2026-09-10: Skipped Workflow Branches And Stale Publisher Code

**Issue:** Release recovery stalled after intentionally skipped jobs. Later,
public registry preflight used an older parser despite a corrected workflow.

**Cause:** GitHub's implicit dependency-success condition suppressed downstream
jobs after a valid alternative branch was skipped. A separate checkout selected
immutable source code where current publisher authority code was required.

**Repair:** Explicitly recognize only the permitted predecessor result
combinations and retain cancellation handling. Run registry/policy verification
from protected publisher code while keeping package bytes and source evidence
bound to the immutable source revision.

**Verification:** Focused workflow tests and exact-revision hosted qualification
preceded successful protected publication from publisher
`ab9025c2d14743720f209600032b6537574e0ae0`. The failed runs remain failed.

**Prevention:** Test fresh-publication, exact-state recovery, intentional skip,
failure, and cancellation paths separately. Record source and publisher SHAs;
neither identity can silently substitute for the other.

## 2026-09-10: PyPI Visibility Race

**Issue:** Run `34473404830` uploaded successfully and immediately failed with
`release_pypi_missing`. Both exact public files became visible shortly afterward.

**Cause:** Upload acceptance and public index visibility are separate events.

**Repair:** A fresh protected run revalidated exact public bytes and safely
skipped duplicate upload. The local follow-up adds up to six post-upload
observations, retrying only absent or partial-but-exact file sets. Backoff is
`1, 2, 4, 8, 15` seconds: 30 seconds of scheduled sleep plus request time, not
a 30-second wall-clock deadline. Each HTTP operation retains its 20-second
socket timeout; the workflow retains a five-minute outer limit.

**Verification:** Regression tests cover absence-to-partial-to-exact convergence,
bounded exhaustion, conflicts, invalid retry mode, and placement only in the
post-upload verifier. Preflight remains one-shot. The recovery run is publicly
complete; the new retry implementation is a local follow-up, not part of the
immutable `0.19.1.post1` package.

**Prevention:** Retry an explicitly classified transient state, never arbitrary
exceptions. Stop immediately on identity, file, digest, size, type, yank, JSON,
transport, or TLS failures. Expiration does not justify rewriting artifacts or
weakening release gates.

## 2026-09-10: Malformed Registry Responses

**Issue:** A digest map containing only `md5` escaped schema validation and raised
`KeyError`. An empty HTTP 200 response was retried as if it were a missing version.

**Cause:** A strict-subset comparison did not require the `sha256` key. The HTTP
adapter also confused malformed successful content with its HTTP 404 sentinel.

**Repair:** Require `sha256` explicitly before indexing it. Reject empty HTTP 200
content with `release_pypi_json`; reserve absence for actual HTTP 404 responses.
Explicitly close HTTP-error responses as well as successful response streams.

**Verification:** Both defects reproduced before repair. Three focused tests
passed afterward, including the real HTTP adapter's 404-to-partial-to-exact path
and response closure. The combined release-authority and daemon suites passed
all 250 tests. The final full-suite result is recorded below.

**Prevention:** Test malformed required fields with unrelated extra keys. Exercise
the transport adapter as well as injected state-machine fixtures. A closed error
must remain a diagnostic rejection, not a traceback or a retry candidate.

## 2026-09-10: Published Version With Candidate Wording

**Issue:** README and release headings still described an unpublished candidate
after `0.19.1.post1` was public.

**Cause:** Executable version and artifact checks did not establish that the
packaged long description reflected the completed publication state.

**Repair:** Correct source README/changelog/release notes and add the dated
publication receipt. Preserve prior failures as historical evidence.

**Verification:** Re-read live GitHub and PyPI state before editing. Confirmed
non-draft, non-prerelease, immutable GitHub state and matching public files.

**Prevention:** Review public-facing metadata before building. Source documentation
changes do not rewrite an already-published wheel, sdist, or PyPI description;
these wording corrections require a future qualified package release.

## 2026-09-10: Daemon Stopped Since August 19

**Issue:** The current CLI reported that the daemon was not running.

**Confirmed evidence:** The retained local log records SIGTERM on August 19,
followed by successful connection drain and a clean shutdown. The sender of that
signal is unknown. This is not evidence of a crash or an Echo credential failure.
No Algo launchd agent was installed. The Phase 1 daemon does not auto-start,
auto-route normal CLI work, or run background refresh workers.

**Repair:** Start the installed `0.19.1.post1` daemon using `algo-cli daemon start`.
No daemon source change, permission relaxation, credential reset, or Echo-policy
change was needed.

**Verification:** The detached process survived the launcher exit. Status reported
ready, protocol 1, and the installed application version; `health_check` returned
true and an owner-socket `ping` returned `pong`. The daemon regression suite also
passed with the release-authority tests. `workers_running=0` is consistent with
the implemented Phase 1 scope, not proof that background refresh is active.
Repeat-start reported the same running instance, and a later status check showed
that instance still healthy after more than ten minutes.

**Prevention and limits:** Check live status, process identity, shutdown logs,
service registration, and the implemented contract before blaming an old error.
A successful restart is not persistent self-recovery. Automatic login/reboot
restart remains unconfigured and requires a separately scoped lifecycle change.
Normal Algo CLI work remains in-process independently of this optional daemon.

## 2026-09-10: Qualification Environment And Evidence Currency

**Issue:** The first full local run returned 25 failures, 6,007 passes, and 41
skips. The release/daemon-focused suite had passed, so that narrower result was
not sufficient evidence for the full harness.

**Cause:** Twenty-four failures traced to the missing pinned Echo dependency in
this review worktree. One rejected an M8 report whose source digest belonged to
the previous tree. The old dev-extra setup instructions did not describe the
full qualification environment. After M8 regeneration, M9 correctly rejected
the old ledger binding with `m9_audit_local_digest` until it was refreshed too.

**Repair:** Sync the workflow's frozen non-editable environment with `dev`,
`supply-chain`, and the `echo-veil` dependency group. Verify the exact pinned
source and installed RECORD, and installed-source parity. Run the real M8
qualification, append its measured digest and limits to the evidence ledger,
and regenerate M9 through its auditor. Update `AGENTS.md` and the release skill
with this setup and ordering. No test assertions, thresholds, or skips changed.

**Verification:**

- `ALGO_TEST_REQUIRE_RIPGREP=1 .venv/bin/pytest -o addopts= -q --tb=short tests`:
  6,032 passed, 41 skipped, 98.53 seconds.
- `ruff check algo_cli tests scripts`: passed.
- `henry_echo_veil_dependency_audit.py`: passed at pinned Echo source
  `cbee525687ac03c830d4b6632ff1d044b4b838fc`.
- `oliver_installed_source_parity.py`: passed, with no missing or divergent files.
- `henry_m8_qualification.py`: nine local metrics passed, five external-browser
  metrics still blocked, zero failed; public-claim eligibility remains false.
- `arthur_m9_completion_audit.py --verify-report
  hardening/ada-m9-completion-audit.json --expect-blocked --quiet`: passed its
  evidence-currency check while retaining the broader blocked status.
- `david_hardening_gate.py`, `nathan_agent_runtime_qualification.py --quiet`,
  `check_public_release.py`, and `check_release_version.py`: passed.
- The updated release-upgrade skill passed its structural validator.

**Prevention and limits:** Audit the declared environment before the full suite;
classify actual failures instead of treating cache history as current evidence.
Recompute source-bound reports after a change using their runners. Local passes
do not qualify a new hosted release, native platform, or external-browser matrix.
The follow-up remains separate from the published immutable package.

**Follow-up invocation check:** A later full run using
`.venv/bin/python -m pytest` from this non-editable checkout produced 40 protected
search failures, 6,010 passes, and 41 skips. The parent imported source files,
while `python -I` workers imported site-packages; the unchanged runtime-root
identity check correctly rejected the mismatch. Both import paths were observed
directly. Using the workflow's `.venv/bin/pytest` entrypoint passed all 55
applicable protected-search tests, with one native-Windows skip. Do not relax
the worker guard or inject PYTHONPATH to make mixed-runtime tests pass. Match
the installed qualification entrypoint as well as the dependency set.

## 2026-09-10: Upgrade Fixture Resolved The Public Package

**Issue:** All three installed-wheel jobs in
[CI run 34484392811](https://github.com/Seabass-up/Algo-cli/actions/runs/34484392811)
failed the pipx/uv baseline check before reaching Algo's updater. The local
reproducer installed public `0.19.1.post1` instead of the required `0.18.0`.

**Cause:** The fixture used `UV_NO_INDEX`, which uv 0.11.26 does not support.
uv also does not inherit pip's index controls. A local find-links directory alone
did not exclude PyPI. Publication of a newer version exposed this dependency on
public registry state. A separate receipt defect initialized
`published_updater_exercised=true` before any updater call had run.

**Repair:** Use the supported `UV_OFFLINE=true` control with a new isolated
cache for each run. Keep pip's own no-index setting and use an escaped file URI
for both installers' wheelhouse location. Verify fresh-process package and
metadata locations, then compare every original wheel payload file except the
installer-rewritten RECORD. Cover both `algo_cli` and the shipped `ollama_cli`
compatibility package; reject mismatches, missing files, unexpected package
payloads, and links. Record verified file counts after each successful phase.
Set the updater-exercised flag only after its command completes successfully.

**Verification:** The original baseline failure was reproduced locally with the
exact CI candidate. After repair, local macOS runs passed for pip, pipx/pip,
pipx/uv, and uv tool: 251 baseline files and 329 candidate files matched, including
after repeat update. All 11 synthetic state files retained bytes, modes, and
modification times, with SQLite integrity checked. The candidate wheel SHA-256
was `a6058046d23786287875f2eb4f91ed1b3cdc3899f4e20c67f2c9a4d9a3ff7ead`,
distinct from the same-version public wheel's
`3aca3f8ec689f98b6572ff1fcd5474de3984b4f1b9b4c6b9ef47c45e48f88678`.
The false updater-execution claim also failed its regression before repair.
The focused install/upgrade suite passed 43 tests afterward. The correctly
invoked full installed suite passed 6,050 tests with 41 existing skips in 88.73
seconds; lint, pinned Echo audit, installed-source parity, hardening gate, and
M9 evidence-currency verification also passed. Local receipts are
under `/private/tmp/algo-cli-pipx-baseline.S5QzkC/`; hosted qualification of the
corrected revision is still pending.

**Prevention and limits:** Check the pinned installer's actual supported controls
and test all manager/backend combinations. Do not assume pip-compatible command
syntax implies compatible environment variables. A version-only assertion can
accept different builds; bind installed payloads to the candidate, not an
installed RECORD or the public version label. Initialize execution claims false
and advance them only on observed outcomes. These are delivery-fixture changes,
not a repair to the user's normal updater or a new release. Local synthetic-state
tests do not qualify native Windows or real OS keychain preservation. See the
[uv environment reference](https://docs.astral.sh/uv/reference/environment/#uv_offline)
and [pip compatibility notes](https://docs.astral.sh/uv/pip/compatibility/#configuration-files-and-environment-variables).

## 2026-09-10: Standalone uv pip Install Was Misclassified as pip

**Issue:** `algo-cli update` failed with exit 1 because the running virtual
environment had no `pip` module. The affected installation was
`algo-cli-runtime 0.19.1.post1` under a regular virtual environment created by
`uv pip`, rather than a `uv tool` environment.

**Cause:** Manager inference only recognized pipx and `uv tool` directory
layouts and otherwise selected `python -m pip`. It ignored the installed
distribution's `INSTALLER=uv` metadata, so a valid pip-free `uv pip`
installation was assigned to the wrong updater.

**Repair:** The current Mac installation received a one-time pip bootstrap via
`uv pip`, after which its published updater completed successfully and reported
the installed `0.19.1.post1` version. Unreleased source now distinguishes
standalone `uv pip` from `uv tool`, emits a fixed `uv pip install --python`
command, and stops with an explicit error if the owning `uv` binary is absent.
Path-owned pipx and `uv tool` environments continue to take precedence over
installer metadata. The cross-platform package smoke matrix now includes a
pip-free standalone `uv pip` environment and records that older packages need
an owning-manager bootstrap before the repaired updater is installed.

**Verification:** The pre-repair focused suite produced 11 expected failures.
After the source repair, `tests/test_updater.py` and
`tests/test_oliver_smoke_upgrade.py` passed 71 tests. A live source probe against
the affected interpreter selected `uv-pip` and emitted the expected absolute
interpreter command. The normal `algo-cli update` launcher then completed with
exit 0 and reported `v0.19.1.post1`. All five macOS upgrade paths passed against
the candidate wheel, preserving 11 synthetic state files and matching 251
baseline plus 329 candidate wheel files. The full non-editable suite collected
6,100 tests and completed successfully with existing platform skips. Its first
coverage run met the 57 percent floor at 67.37 percent and exposed a stale M9
artifact; M8, Nathan, the evidence ledger, and M9 were refreshed, the exact M9
regression passed, and the clean full-suite rerun passed. Ruff, focused mypy,
compile, public-source, version, dependency, parity, hardening, and M9 gates
passed. Hosted macOS/Linux/Windows qualification remains pending; this source
repair is not yet published.

**Prevention and limits:** Installation ownership is not determined by virtual
environment path alone. Check both manager-specific paths and distribution
installer metadata, and qualify every supported install topology without
assuming pip is bundled. A source fix cannot repair an already installed older
updater until an owning-manager command or equivalent one-time bootstrap gets
the corrected package onto that environment.

## 2026-09-10: Non-Editable Test Run Could Not Import Qualification Scripts

**Issue:** The workflow-equivalent non-editable pytest invocation stopped during
collection because tests importing `scripts` could not resolve that namespace.

**Cause:** A pytest console entry point starts with its virtual-environment bin
directory on `sys.path`. The repository root was not otherwise available in the
fresh non-editable environment. Adding it at the front would have caused parent
tests to import source `algo_cli` while isolated workers imported the installed
copy, violating the runtime-root identity check.

**Repair:** The shared test bootstrap appends, rather than prepends, the checkout
root. Qualification scripts become importable while installed packages retain
precedence.

**Verification:** The workflow-equivalent focused suite passed 71 tests after
the change. The full non-editable suite collected 6,100 tests and completed
successfully with existing platform skips. The refreshed M8 focused adversarial
cell passed, and installed-source parity reported no missing, divergent, or
unexpected Python files. Hosted CI remains required for native Windows and
Linux confirmation.

**Prevention:** Reproduce CI with a fresh non-editable environment and preserve
module precedence when exposing repository-only test utilities. Do not use a
front-loaded `PYTHONPATH` to hide an installed/source runtime mismatch.

## Repair Log Checklist

- Date and component.
- Observed symptom and confirmed cause; identify anything still unknown.
- Exact repair and its local, deployed, or operational scope.
- Verification commands, results, and evidence links.
- Prevention or regression coverage, plus remaining limits.

Never include credentials, personal state, raw sensitive logs, or unsupported
success claims. A failed check remains a failed check even after later recovery.
