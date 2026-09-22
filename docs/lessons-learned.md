# Lessons Learned

Record verified repairs here before closing the associated task. Update an
existing incident instead of duplicating it. Each entry needs an observed issue,
confirmed cause (or an explicit unknown), repair, verification, prevention, and
remaining limits. These are dated receipts, not proof of future runtime state.
The [changelog](../CHANGELOG.md) and [release history](henry-release-0.19.1.md)
retain the earlier reliability, model-discovery, Echo, Windows, and browser
qualification repairs; this log adds the verified release-closeout lessons.

## 2026-09-13: Pre-PyPI Snapshot Expiry Misread As Asset Digest Mismatch

**Issue:** Protected publisher run
[34793549112](https://github.com/Seabass-up/Algo-cli/actions/runs/34793549112)
failed at `Recheck and revoke policy authority immediately before PyPI` with
opaque exit 2 / `release_pre_pypi_authority`. The failure was read as a draft
asset digest+size mismatch. PyPI `algo-cli-runtime/0.19.2` and `algo-cli/0.19.2`
stayed 404.

**Cause:** The before-pypi snapshot was captured at `2026-09-14T00:54:10Z` and
the publish job's `release-authority` approval plus setup reached the freshness
check at `2026-09-14T00:56:30Z` (140s). The 120-second window failed first. All
17 draft assets matched retained local bytes: GitHub `digest` is
`sha256:` plus lowercase hex and compared equal to local SHA-256, including
sizes. The asset compare never ran. v0.19.1 Draft and immutable Latest
`v0.19.1.post1` were not touched.

**Repair:** Keep the 120-second snapshot bound and the digest+size equality
check. Report `release_draft_snapshot_expired` with age when the snapshot is
stale, and name the exact local/remote digest+size pairs when those differ.
Do not recapture, widen the window, or upload.

**Verification:** Live draft 388084902, same-run snapshot, and retained
`oliver-verified-release-assets-34793549112-1` compared equal for every asset.
Focused authority tests cover 119s pass, 121s/140s expiry, and named digest
mismatches.

**Prevention:** Read the snapshot age before assuming a digest mismatch. A retry
must be a new `workflow_dispatch` attempt 1. Approve the publish job's
`release-authority` environment (URL `https://pypi.org/p/algo-cli-runtime`)
within 120 seconds of `draft-publish-capture` completing. Do not re-run the
failed attempt.

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
installation was assigned to the wrong updater. The first repair also treated
every remaining `INSTALLER=uv` environment as standalone, which would have
misclassified a `uv tool` installation under a custom configured tool directory.

**Repair:** The current Mac installation received a one-time pip bootstrap via
`uv pip`, after which its published updater completed successfully and reported
the installed `0.19.1.post1` version. Unreleased source now distinguishes
standalone `uv pip` from `uv tool`, emits a fixed `uv pip install --python`
command, and stops with an explicit error if the owning `uv` binary is absent.
Path-owned pipx and standard `uv tool` environments continue to take precedence
over installer metadata. For custom layouts, detection reads `UV_TOOL_DIR` or
queries `uv tool dir` with a fixed, bounded command before selecting standalone
`uv pip`. The cross-platform package smoke matrix now includes a
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
installer metadata, including the package manager's configured ownership root,
and qualify every supported install topology without assuming pip is bundled.
A source fix cannot repair an already installed older updater until an
owning-manager command or equivalent one-time bootstrap gets the corrected
package onto that environment.

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

## 2026-09-13: Refresh Boron Chrome Security Pin and Coupled Fixtures

**Issue and cause:** The source still pinned Chrome `151.0.7922.108` after the
user verified Linux stable `153.0.8010.36`. Initial pin-refresh tests exposed
coupled fixtures: browser major 151 failed hosted validation, and an older
wrong-version fixture triggered feed regression before the intended lag check.

**Repair:** Updated build/qualifier versions, release milliseconds, Dockerfile
package URL/checksum/labels, SBOM package identity, fixtures, and current-pin
docs. Used `153.0.8010.37` for the newer-but-stale release case and major 153
for hosted evidence. Preserved the 72-hour limit, lag checks, and isolation
fixtures including `151.0.7922.34`.

**Verification:** `shasum -a 256` on the supplied local Debian package matched
`9bb44e33031c2f2857cf36b4343051a12f93058e4b781e3c76313df87f6c8d32`.
`uv run pytest tests/test_boron_browser_images.py tests/test_boron_browser_isolation.py tests/test_henry_boron_hosted_qualification.py -q`
passed with 435 passed and 1 skipped after correcting the fixtures.
`git diff --check` passed. The parser test verified the supplied serving time
maps to `1_788_902_443_154` using the build script's millisecond semantics.

**Prevention and limits:** Refresh the complete package identity and coupled
release/major fixtures together; keep negative cases on their intended rejection
path. This is local source validation using user-supplied live release evidence,
not a hosted image build, qualification, publication, or deployment. The durable
prevention lesson is in the global `wiki-general/lessons-learned/cross-cutting.md`.

## 2026-09-13: New Release Could Not Use Fixed Prior Draft Identity

**Issue:** The protected draft-capture child could authorize only the historical
`v0.19.1.post1` tag, source revision, release ID, and distribution filenames, so
it could not capture a newly created `v0.19.2` draft. Review then found that the
first generalization incorrectly required the immutable release source to equal
the current publisher SHA, blocking exact-asset recovery after `main` advanced.

**Cause:** The least-privilege recovery path correctly fixed the prior release's
identity, but GitHub assigns each new draft a new numeric release ID. Carrying
that old ID into the next release made the otherwise reusable publisher reject
the new draft. A release's tagged source and a later protected-main recovery
publisher are also distinct identities and must not be collapsed.

**Repair:** Bind capture to exact tag `v0.19.2`; bind the publisher to the
protected-main workflow SHA; and preserve the release's exact lowercase commit
target as its source. Derive the numeric release ID from exactly one matching
row in the bounded release listing, require a positive non-boolean integer, and
allow only that detail endpoint and the validated asset endpoints. The read-only
validator repeats the tag, separate source and publisher SHAs, listing, release
ID, receipt-age, and endpoint bindings before authority resolves the tag.

**Verification:** The complete release-authority focused group passed after a
frozen non-editable environment refresh, covering dynamic release IDs, missing,
ambiguous, truncated, and drifted listings, malformed identities, credential-free
signed-asset redirects, exact filenames and digests, source binding, native
version mapping, updater smoke fixtures, and a later-main recovery publisher
with an unchanged tagged source. `git diff --check` also passed.

**Prevention and limits:** A fixed recovery identity is not a reusable release
identity. For each release, keep the tag allowlist exact and derive only the
server-assigned ID from a unique bounded observation. Keep tagged source and
current publisher separate, require both to be exact commit SHAs, and prove the
ancestor and CI relationships before recovery. This is a tested local repair
and prepared `0.19.2` candidate; hosted qualification and public publication
are still required before calling it released or upgradeable.

## 2026-09-16 - User-Only YOLO Activation and Preapproval Isolation

**Component and symptom:** Session modes, runtime grants, and B51 permissions.
The research wiring spec could not edit the runtime, and assumed a live editable
install. `/mode yolo` was absent. The active command actually imports a
non-editable `0.19.1.post1` release environment. B51's helper replaced explicitly
empty tool/spawn sets with default permissions. Team delegation deep-copied
runtime Config attributes, including authority locks once preflight ran.

**Confirmed cause:** Posture enums had no YOLO entry; compatibility capability
tiers are not grants. Empty and omitted allowlists used the same truthiness
test. A first implementation allowed copied or delegated configurations to
reuse a parent's prepared preapproval, which the new transition tests exposed.

**Repair:** Add explicit interactive user activation bound to its Config owner
and workspace, exclude activation from saves/copies, suppress it during agent
execution, and issue distinguishable one-use preapprovals rejected outside the
owner's active mode. Preserve action-time approval, safe mode, Echo Veil,
workspace restrictions, and completion verification. Deny credential actions
and sensitive typed paths in YOLO. Copy declared Config fields for specialists
and explicitly preserve their review mode/channel. Preserve empty B51 sets
using `None` for omitted defaults. Ship a bounded posture resource and a
discoverable skill; correct B51's wildcard and enforcement claims.

**Verification:** The 18 new boundary tests and the 224-test focused runtime
group passed. Repository-wide Ruff passed; mypy passed for 11 affected source
files. Frozen non-editable installation passed installed-source parity and
the pinned Echo dependency audit. Build of the source archive and wheel from
that archive succeeded, and Twine accepted both. The isolated installed-package
smoke exercised `/mode yolo`, `/mode status`, system-prompt injection, restart
and tool-entry denial, `/harness refresh`, `/harness status`, skill search/read,
and mode exit without changing the user's config.
The final `ALGO_TEST_REQUIRE_RIPGREP=1 .venv/bin/pytest tests
--junitxml=/tmp/algo-yolo-final-suite.xml` run passed 6,097 tests with 41 skips
and no failures (111.77 seconds), including current evidence checks.
The Nathan runtime receipt passed 17/17 correctness probes and every gate;
the regenerated M8 receipt passed 9 local metrics with 5 external metrics
blocked and none failed. M9 accepted the fresh ledger bindings with
`--expect-blocked`; no external requirement was marked complete.

**Prevention and limits:** A prompt or wildcard cannot grant authority. Check
the installed import, test transitions using already-prepared grants, and
distinguish omitted permissions from explicit denial. Record measured benefit
separately from boundary correctness. At this initial YOLO qualification stage
the repair was local to the worktree and the ordinary PATH command had not been
replaced. Hosted platforms have not been qualified, and no YOLO performance or
provider-quality gain has been measured.
The isolated harness smoke used keyword retrieval with embeddings pending.

**Backend correction (2026-09-16):** The handoff incorrectly described Echo
Veil as the current host's memory backend. Live config has
`echo_veil_enabled=false` and `echo_veil_protection=optional`. The installed
D-57 skill and `~/.local/bin/d057-algo-cli` provide a CLI/skill bridge;
`d057-algo-cli doctor` passed store verification at sequence 39 with the key
and tip anchor in Keychain. Echo compatibility tests do not establish the
active host backend. No config, memory store, or launcher was changed.
The saved config currently lacks the D-57 attachment fields, and the installed
Config declares none; its dataclass-only save cannot preserve those undeclared
fields. The specific event that removed them is unknown. Built-in memory
routing to D-57 remains unverified; the healthy bridge alone does not prove it.
Before further runtime changes, qualify the selected D-57 path rather than
re-enabling Echo or inferring integration from a skill, shim, or static label.

**Follow-up repair:** Declare and persist explicit D-57 settings and route
Config/tool/slash memory writes, native `/memories`, and prompt recall through
the existing shared CLI. Reject competing authority selection, invalid flags,
unverified projections, backend failure, and legacy catalog mutation without
fallback. Serialize Algo snapshot updates, verify readback, and never retry an
uncertain append automatically. Keep old memory files untouched and do not
silently migrate or inject them. Suppress plaintext Intuition and alternate
continuity writes; retain action-time authority policies and child isolation.
The build is labeled `0.19.2+local.d057.yolo.20260916`, not a public release.

**Shared writer repair:** The D-57 CLI previously replaced the archive
atomically without serializing its archive/key/tip transaction. The shared
entrypoint now uses a cooperative store lock for all commands. Its 45-test
suite passed, including 16 concurrent Algo/Pi appends, verification of all 17
records including bootstrap, and recall of every task. The raw codec API is
outside this CLI concurrency guarantee; Windows locking remains unqualified.
See the D-57 package's `docs/lessons-learned.md`.

**Follow-up qualification:** The focused Algo group passed 172 tests with
21 skips. The isolated installed-wheel probe passed native write/recall,
restart, `/lesson`, `/memories`, YOLO, harness refresh, and corrupt-tip refusal
without touching live memory or creating plaintext memory files. Source parity,
repository Ruff, scoped mypy (9 files), source-archive/wheel build, and Twine
checks passed. M8's first refresh exposed a production-native-release fixture
that improperly used the local CLI version; the fixture now uses a release
version and an explicit test retains local-build rejection. The packager was
not weakened. The final installed-wheel run, `ALGO_TEST_REQUIRE_RIPGREP=1
~/Code/Algo-cli-local-20260916-d057-yolo/.venv/bin/pytest
tests --junitxml=/tmp/algo-d057-final-suite.xml`, passed 6,119 tests with
41 skips, no failures, and current postwrite evidence checks (124.04 seconds).
The refreshed Nathan receipt passed 17/17 correctness probes. The final M8
receipt passed 9 local metrics with 5 external metrics blocked, none failed;
M9 accepted the refreshed bindings while retaining 13 blocked requirements.

**Local activation and live refresh:** The normal `~/.local/bin/algo-cli`
now points to the separately installed exact wheel at
`~/Code/Algo-cli-local-20260916-d057-yolo/.venv/bin/algo-cli`.
`algo-cli --version` reports `0.19.2+local.d057.yolo.20260916`.
Private config and old-launcher backups were created before enabling the four
D-57 settings. Echo remains disabled and optional; the saved workspace is
preserved. A read-only installed-runtime probe passed D-57 doctor and prompt
recall, refreshed the real harness, and found/read the new YOLO skill under
protected retrieval. It observed sequence 41 unchanged across the probe and
unchanged configuration, monitored credential files, and memory-store bytes;
no live memory write was performed. Sequence 39 above is the earlier doctor
observation, not a claim that no other actor has appended since then.
The eligible index has 789 records, with 220 embeddings ready and 569 pending.
YOLO remains user-activated and session-only; restart did not activate it.

**Delivery limits:** This is an activated local development build, not a
GitHub/PyPI release. The previous release environment remains intact, and
already-running CLI processes need restarting to load the new code. Legacy
memory was not automatically imported. External M8 browser requirements,
Windows D-57 locking, raw-API concurrency, and daemon recovery were not
qualified. Harness refresh indexes resources; it does not install runtime code
or prove a measured effectiveness gain.

## 2026-09-16 - YOLO Still Prompted and Completion Recovery Stopped Early

**Observed symptom and confirmed cause:** The activated YOLO build still
prompted for shell commands. Its implementation only issued preapproval for
actions already classified as session-preapproval; the tests explicitly
expected shell/file prompts instead of the user's observable no-prompt
workflow. Separately, a second premature model answer ended verification
recovery immediately despite the configured four-round recovery window.
New regression tests reproduced both defects. The exact sequence of the user's
failed research run was not recovered, so a rejected verifier is not asserted
as that run's confirmed cause.

**Repair:** Following explicit user authorization, issue independent one-use
YOLO grants for registered session/action-time actions within scope, and
generate exact runtime confirmation receipts from the live user activation.
Retain forced reviews, handoff/credential restrictions, existing path and
safe-mode denials, backend protections, child isolation, and verifier evidence.
Only direct user input changes protection settings. Continue premature-answer
recovery within both the four-round and configured work-iteration budgets;
exhaustion preserves changes and reports the actual verification limit rather
than claiming success.

**Verification observed:** The new actual-dispatch test writes a real file,
runs Python assertions through the real shell tool without requesting input,
and clears the completion gate only after the successful check. Tests cover
concurrent independent grants, forced review, copy/child/exit/workspace
revocation, retained safe mode, later successful recovery, and failed verification.
The isolated installed-package D-57 probe also passes real shell/write dispatch
with input configured to fail if any approval is requested. Repository Ruff,
scoped mypy (4 files), sdist/wheel-from-sdist, Twine, and installed-source parity
passed. Nathan passed 17/17 deterministic probes; M8 passed 9 local metrics with
5 external metrics blocked, none failed; M9 accepted current bindings.
The final exact-wheel suite passed 6,129 tests with 41 skips and no failures
in 129.11 seconds (`/tmp/algo-yolo-approval-fix-final-suite.xml`). Normal
`algo-cli --version` now reports `0.19.2+local.d057.yolo.20260916.1` from the
separate `Algo-cli-local-20260916-yolo-approval-fix/.venv` installation.
Activation passed current parity and M9 verification, preserved config bytes,
and verified live D-57 at sequence 41 without a memory write. The prior launcher
is retained at `~/.local/bin/algo-cli.pre-yolo-approval-fix-20260916`.
The first activation preflight rejected a missing M9 report-path argument before
any mutation; correcting the wrapper argument did not change a qualification
rule. Existing CLI processes must exit/relaunch to load the corrected code.
The final real-home refresh passed protected retrieval of the corrected YOLO
skill and verified D-57 prompt recall. Monitored config, credential, and memory
bytes remained unchanged; the index has 789 eligible records with embeddings
still partially pending.

**Prevention and limits:** Specify user-visible acceptance criteria first:
activate through the interactive dispatcher, execute an actual shell command,
and prove that no approval channel is used. A large passing suite can preserve
the wrong requirement. Test premature answers as well as tool-heavy recovery.
The new local build is `0.19.2+local.d057.yolo.20260916.1`. Workspace-bound
consent is not an OS sandbox: shell processes retain the user's system
permissions. Unknown/unavailable actions are not made supported, and unverified
work is not relabeled as verified. No live memory or research artifact was
changed by qualification, and no paid provider run or public release was used.

### Follow-up - Cross-Workspace Read-Only Actions Had No YOLO Grant

**Component, symptom, and confirmed cause:** Nathan's observation baseline was
cwd-scoped, while YOLO only granted session/action-time policies. Ordinary
read-only actions use `ConfirmationMode.NONE`, so sibling D-177 reads and
listings had no grant. Eight new cases reproduced the denial before repair.
The previous no-prompt tests did not cover cross-workspace observations.

**Repair:** Genuine owner-bound activation now issues independent one-use,
concrete-target grants for curated read-only workspace observations beyond
cwd. Ordinary modes retain cwd-scoped baselines. File mutations remain
workspace-scoped. Sensitive targets, backend safeguards, caller ceilings,
changed-target checks, consumed-grant denial, and child/copy/exit/workspace
revocation remain enforced. Clarify these boundaries in B51, the shipped
YOLO skill, and posture text rather than recommending shell workarounds.

**Verification observed:** Source and exact-wheel focused suites passed
124 tests each. Ruff, scoped mypy (2 files), sdist/wheel-from-sdist, Twine,
and installed parity passed. The exact wheel successfully dispatched real
D-177 report reading, listing, search, and read-only slash aliases from the
saved `codec-research-fork` workspace, with zero prompts. Monitored config,
credentials, D-57 store, report, and seal bytes remained unchanged. Doctor
reported healthy sequence 42 throughout that probe. The isolated installed
D-57 write/recall/restart and corrupt-tip refusal probe passed. Nathan passed
17/17; M8 passed 9 local metrics with 5 external blockers and no failures;
M9 accepted current bindings (29 verified, 13 blocked, none failed).

**Prevention and remaining limits:** Test sibling folders with absolute,
relative, and home-expanded paths through actual dispatch, not raw tools or
mode-only assertions. New grants are read authority, not external-write
permission or an OS sandbox. The final exact-wheel suite passed 6,143 tests,
41 skips, and no failures in 111.92 seconds
(`/tmp/algo-yolo-cross-workspace-final-suite.xml`). Normal PATH was activated
after fresh parity and M9 verification and now reports
`0.19.2+local.d057.yolo.20260916.2` from the separate
`Algo-cli-local-20260916-yolo-cross-workspace/.venv` wheel installation.
Config bytes and the saved workspace were preserved, D-57 remained healthy,
and the prior launcher is retained as
`~/.local/bin/algo-cli.pre-yolo-cross-workspace-20260916`. The post-activation
actual D-177 dispatch probe passed with zero prompts and unchanged monitored
state. Real-home refresh retrieved the corrected skill and D-57 prompt,
preserving monitored credentials/config/memory bytes (sequence 42). There are
789 index records; embeddings remain partly pending. Running CLI processes
must quit/relaunch and re-enter YOLO; refresh cannot replace loaded code.
No public release or paid provider benchmark was performed. External browser
qualification is unchanged, and no model-quality benefit is claimed.

### Follow-up - Action-Program Format Was Misreported as Missing Authority

**Component and observed symptom:** YOLO `action_program` stopped with
`program outputs must be a non-empty list` and `attempts to set runtime-owned
fields: cwd`. Both results misleadingly began `Blocked by runtime authority`
despite valid outer grant/confirmation, and the model claimed noninteractive
trusted approval was unavailable.

**Confirmed cause and evidence boundary:** Omitted/null outputs already
defaulted to the final step, but an explicit empty list did not. The compiler
forbade every nested cwd value, even one matching the active workspace.
Regression programs reproduced both failures before repair. The user's full
plans were truncated: empty outputs and redundant cwd are reproduced causes,
not proof of the hidden values. The reported compiler errors do not establish
the model's claimed approval failure.

**Repair:** Normalize empty outputs to the existing final-step default and
redundant active-workspace/null cwd for runtime-cwd-bound tools before freezing
the program. Preserve the caller's plan and equivalent frozen binding. Retain
conflicting-workspace, cfg/safe-mode, invalid type/reference, caller-ceiling,
effect-finality, and nested-authority checks. Label compiler errors separately
from missing outer authority, and teach the schema, A12b, skill, and prompt
to correct format within existing authority. Write and verifier remain separate
supported version-1 calls; no effect restriction was removed.

**Verification observed:** Source and exact-wheel focused suites each passed
245 tests. Ruff, scoped mypy (2 files), sdist/wheel-from-sdist, Twine, installed
parity, and Nathan 17/17 passed. Actual YOLO dispatch runs shell programs with
omitted/null/empty outputs and writes/verifies a temporary file with matching
nested cwd and empty outputs, using input that fails on any prompt. The
isolated D-57 write/recall/restart/corrupt-tip probe passed those programs too.
Actual D-177 sibling read/list/search dispatch still passed with monitored
config, credentials, memory, report, and seal unchanged (healthy sequence 42).
M8 passed 9 local metrics, with 5 external blockers and no failures; M9 accepted
current evidence bindings. The final exact-wheel suite passed 6,165 tests,
41 skips, and zero failures in 113.40 seconds
(`/tmp/algo-yolo-program-compat-final-suite.xml`). Fresh parity and M9 checks
passed before activation. Normal PATH now launches the separately installed
`Algo-cli-local-20260916-yolo-program-compat/.venv` wheel and reports
`0.19.2+local.d057.yolo.20260916.3`, superseding the prior `.2` local build.
The previous launcher is retained at
`~/.local/bin/algo-cli.pre-yolo-program-compat-20260916`. Activation preserved
config bytes and the saved codec workspace, with D-57 healthy at sequence 42.
Post-activation isolated program write/verification, actual D-177 read-only
dispatch, and real-home harness refresh/retrieval of the updated skill and
D-57 prompt passed. Monitored config, credentials, memory, report, and seal
bytes stayed unchanged in the relevant live probes. The index has 789 records
with embeddings partly pending.

**Prevention and limits:** Qualify model-shaped composite plans through real
outer and nested dispatch, not only direct actions. Distinguish optional-field
normalization, malformed arguments, inner authority, forced review, and missing
verification. Do not auto-retry uncertain effects to repair schema errors.
No S-10 script or its claimed research results were created by qualification,
and no public release or live provider effectiveness test was performed.
Existing CLI processes require a restart after installation; refresh indexes
resources but does not replace loaded code.

### Follow-up - Known write refusals, denied programs, and unbounded YOLO work

**Observed symptom:** In YOLO, `write_file` on an existing path was labeled
`unknown_outcome` and created a retry barrier. A later `action_program` that
reported `denied` still carried an "Unknown outcome" warning. After a failed
script run, the same command was skipped until an unresolved outcome was
reconciled. Independently, the default 24-round `/toolmax` (hard-capped at 128)
stopped long YOLO sessions before the work finished.

**Confirmed cause:** Invoked `UNKNOWN_POSSIBLE` mutations were flattened to
unknown even for controlled `Error:` file refusals and structured program
`failed`/`denied` JSON. Observation did not retire workspace-unknown ledger
entries. The interactive loop always used `min(128, max_tool_iterations)`.

**Repair:** Existing-file writes without overwrite are preflight denials.
Structured program statuses are preserved. File-tool `Error:` results stay
failed, not unknown. A fresh workspace read/list reconciles unknown workspace
effects so the same action may run again. Live YOLO now returns no tool/model
round cap (`work_iteration_limit` is `None`) and no verification-recovery
round cap; children and ordinary modes keep the saved `/toolmax` and four
recovery rounds. Shell mutations still require an explicit workspace verifier.
Saved `max_tool_iterations` is unchanged and applies after YOLO exit.

**Verification observed:** Focused source tests covering YOLO mode, dispatch,
Arthur outcomes, run contracts, progress recovery, and slash/overview helpers
passed, including a six-tool YOLO chat with `/toolmax` 2 and a YOLO run
contract at the unbounded work ceiling. Scoped Ruff and mypy of the touched
modules passed. This is not a full-suite, M8, or hosted qualification.

**Prevention and limits:** Do not treat every `Error:` prefix as a known
no-effect failure; browser adapter losses remain unknown. Unlimited YOLO work
is session-only and is not inherited. Ctrl+C still stops a run. No public
release.

### Follow-up - Release preflight found unsafe slash inspection and capture rules

**Observed symptom:** The release-preflight suite reported that `/memories`
output was not captured, while mutating `/google gmail-draft ...` output was
captured. Review then showed that arguments such as `/host status`, `/model
status`, `/system status`, and `/goal show` could be classified as inspection
even though those handlers treat nonempty arguments as mutations. Scoped mypy
also rejected assigning an optional native context value to `num_ctx`.

**Confirmed cause:** A shared output-capture allowlist had grown to include
whole command families whose read and write forms use the same command name.
The policy engine also reused generic inspection words across handlers with
different argument contracts. Type narrowing occurred through a helper that
mypy could not prove at the assignment site.

**Repair:** Restore `/memories` capture and classify output per command and
argument. Empty `/host`, `/keepalive`, `/model`, `/system`, and `/theme` calls
are read-only; `/goal status` is read-only; mutating or ambiguous arguments are
not captured and still require the normal policy path. Keep the existing
command-specific `/config`, `/google`, `/kernel`, and `/plugins` rules. Assert
the native context is present before assigning the already-validated value.

**Verification:** Adversarial tests cover the misleading `status`, `show`,
`clear`, `resume`, and free-text forms, plus output capture for both read-only
and mutating commands. The first candidate-wide run passed 6,230 tests with 41
skips and failed only the intentionally strict stale-M8 check after the release
version and workflow changed. Alice was regenerated with 10 successful
publish/kill/restart recoveries; Nathan passed 17/17 probes and 31/31 workloads
with zero policy escapes, duplicate mutations, or unverified completions; M8
passed 9 local metrics with 5 external metrics still blocked and none failed or
unverified; M9 accepted the current bindings with 29 verified, 13 blocked, and
none failed. The exact installed `0.20.0` source parity check found 286 Python
files with no divergence. Ruff, compileall, public-source scan, the locked
dependency audit, the legacy compatibility dependency audit, and mypy over 77
source files passed. The final suite passed 6,231 tests with 41 skips and no
failures in 104.87 seconds. Hosted qualification and public release remain
pending and must not be inferred from these local checks.

**Prevention and limits:** Read-only status is a handler-specific contract, not
a property of an argument word. Any command family added to output capture must
include negative tests for its mutating forms. Generated qualification JSON
must be written with the runner's explicit `--output` option; stdout from a
passing run does not update the checked-in receipt. Provider-shaped fake tokens
are also public-source and public-history findings. Use non-provider-shaped
placeholders before committing fixtures. The first local feature commit retained
such a fake in its blob history, so the release candidate was rebuilt as one
reviewed squash on `release/v0.20.0`; a single-branch clone passed the history
scan. The unpushed recovery branch remains local and must not be pushed.

## 2026-09-16 - Saved 131k Stamp Capped DeepSeek V4.1 Flash; /reload Dropped YOLO

**Component and symptom:** After the catalog listed DeepSeek V4.1 Flash as a
1M native window, the live footer still showed `cap 131.1k`. `/reload` then
left the session in `execute` even when the user had typed `/mode yolo`.

**Confirmed cause:** Saved `num_ctx: 131072` (a leftover V3.1/remote stamp)
was treated as an explicit `/ctx` override and beat the native 1,048,576
window. `/reload` copied disk `session_mode: execute` onto the live config.
Separately, `reload_runtime()` reloads `algo_cli.session_mode`, replacing
`_YoloActivation`; an `isinstance` check against the new class dropped a
still-valid this-process marker.

**Repair:** Remote catalog stamps smaller than the live native window no longer
cap cloud/xAI/ChatGPT models; custom `/ctx` values that are not those stamps
are kept, and local GGUF allocations stay conservative. Live YOLO is skipped
when copying disk `session_mode`, re-bound after the module reload, and
recognized by duck-typed activation fields so class identity is not required.
YOLO remains session-only and is not persisted or inherited.

**Verification observed:** Focused source tests covering model profile, model
info, context accounting, YOLO activation, and `/reload` passed. Scoped Ruff
of the touched modules passed. This is not a full-suite, M8, or hosted
qualification. Existing CLI processes require a restart after installation.

**Prevention and limits:** Footer and compaction must use
`effective_context_limits`, not raw `cfg.num_ctx`. Do not treat `/reload` as a
session-mode reset. No public release.

## 2026-09-16 - Alice Crash Qualifier Assumed the Old SwiftPM Bundle

**Component and symptom:** Native Alice process-kill qualification failed with
`alice_crash_process_identity` in both M8's focused suite and the broad suite.
The initial broad run had 6,090 passing tests, 41 skips, one failure, and two
postwrite evidence checks deferred. No failed metric was counted as passing.

**Confirmed cause:** This Mac's toolchain defaults to `swiftbuild`, launching
`swiftpm-testing-helper` with the declared `AustinCoreTests.xctest` bundle.
The ownership checker only recognized the older native SwiftPM aggregate
`AustinNativeControlPackageTests.xctest`. A live owned-publisher diagnostic
showed both normal and wide `ps` output; truncation was not the cause.

**Repair:** Recognize the two known test bundles only when a resolved command
argument points into this checkout's `.build` tree. Preserve the publisher
ancestry check before SIGKILL, reject foreign/symlinked-outside bundles and
unknown/malformed commands, and exclude bundle-like ancestors outside `.build`.
Bind `Package.swift` into the crash receipt's source digest.

**Verification:** The focused crash-test group passed, including the actual
process-kill/restart test. Repository-wide Ruff and scoped mypy passed.
`scripts/henry_austin_alice_crash_qualification.py --trials 10` regenerated
the DEBUG receipt with 10 publishes, 10 process kills, and 10 orphan recoveries.
`scripts/henry_m8_qualification.py` then passed all 9 local metrics, with
5 external metrics blocked and none failed. The fresh artifacts are bound in
the evidence ledger; `scripts/arthur_m9_completion_audit.py --write-report
--expect-blocked --quiet` succeeded without lifting the external blockers.
The final full suite passed 6,097 tests with 41 skips and no failures; the
current crash receipt and postwrite evidence checks were included.

**Prevention and limits:** Test-runner identity depends on the build engine.
Validate known products and owned paths plus ancestry instead of accepting any
XCTest-looking process, or weakening the ownership check. This is DEBUG test
process evidence; it does not qualify a signed production service, TCC,
Keychain, power-loss durability, or secure erasure.
## 2026-09-20: Sticky Footer Was Silently Disabled and Broke the Hosted Suite

**Issue:** PR #64's generation footer passed its four focused tests but did not
activate on a real terminal. The Ubuntu suite later stopped inside pytest's own
progress reporter, source-bound qualification was stale, and the dependency
audit rejected `anyio 4.13.0`. Post-push review also found that redirected JSON
runs could emit terminal controls, narrow terminals repainted continuously, the
plain footer omitted the runtime-cap warning, and abrupt POSIX exit or suspend
could strand a restricted scroll region in the caller's shell.

**Confirmed causes:** The implementation read `os.terminal_size.rows`, although
the real API exposes `lines`; `start()` swallowed the resulting `AttributeError`
and disabled the footer. The tests supplied a fake `rows` attribute and patched
the process-wide `shutil.get_terminal_size`, which leaked into pytest reporting.
Resize repainting also did not clear the old footer row. Terminal dimensions
were queried through the process default rather than the selected output
stream, and refresh compared untruncated input with stored truncated output.
The wrapper did not distinguish a JSON sink, and the signal path initially used
buffered stderr recursively; an immediate SIGTERM could interrupt that stream
write and prevent cleanup. Separately, CI reported CVE-2026-63374 and
CVE-2026-64847 in `anyio 4.13.0`, fixed in `4.14.2`. The local Swift toolchain
used the owned `AustinCoreTests.xctest` bundle while the crash qualifier
recognized only the older aggregate bundle name.

**Repair:** Use the real `terminal_size.lines` contract and the selected stream's
file descriptor, keep generation output inside the reserved scroll region,
clear the prior row on resize, compare equally truncated output, and patch only
the module-local size helper in tests. Suppress the footer for JSON sinks, mirror
the runtime-cap chip, require VT support, and restore terminal margins before
SIGTSTP, SIGTERM, and SIGHUP. The emergency reset uses unbuffered descriptor I/O
plus a bounded drain/settle step so an interrupted buffered write cannot swallow
it. Bind the new runtime module and tests into Nathan and M8 source manifests.
Refresh the lock to `anyio 4.14.2` on supported Python versions. Recognize both
known Swift test bundles only when a resolved argument remains inside Austin's
`.build` tree and publisher ancestry matches.

**Verification:** The non-editable installed/source parity check passed with 285
Python files and no divergence. The exact dependency audit reported no known
vulnerabilities. The ten-trial Alice process-kill/restart receipt passed. Nathan
passed 17/17 probes and 31/31 workloads with no policy escapes, duplicate
mutations, or unverified completions. M8 passed all 9 local metrics while
retaining 5 external blockers and no failures. The current final suite passed
6,132 tests with 41 platform skips and no failures/errors in 96.032 seconds;
repository Ruff, mypy over 288 source files, and compileall passed. Ten
consecutive immediate-SIGTERM pseudo-terminal trials observed both footer paint
and terminal-margin reset before process exit. M9 accepted the current Nathan,
M8, and ledger bindings while preserving 13 known external blockers. Hosted
checks for the next pushed revision remain the authority for Linux, Windows,
and GitHub policy state.

**Prevention and limits:** Exercise real standard-library return types, avoid
monkeypatching shared stdlib modules in tests, bind every new runtime path into
qualification manifests, and treat dependency advisories as independent gate
failures. Use a real pseudo-terminal for terminal lifecycle checks; a fake TTY
does not cover signal-time buffered-I/O races, every terminal emulator, resize
race, multiplexor, or remote shell. The repository still carries an optional
Echo Veil dependency for legacy qualification even though Echo is retired as a
memory authority. This repair made no Echo memory call or write; removing that
legacy CI/runtime integration remains separate continuity-migration work.

## 2026-09-20 - Upgrade Qualification Used a Stale Predecessor and Bypassed `uv-pip`

**Component and symptom:** The `v0.20.0` delivery preflight initially passed
upgrade tests from published `0.18.0`, even though PyPI's current predecessor is
`0.19.2`. After correcting that pin, the pipless `uv-pip` case failed because
the smoke test expected the installed updater to identify the environment as
`pip`, then replaced the package through a test-owned `uv pip install` command.

**Confirmed cause:** The release fixture's version, wheel URL, digest, and size
were never advanced after the `0.18.0` release. Its special `uv-pip` branch
preserved a one-time bootstrap workaround for behavior that public `0.19.2`
already fixed, so that branch did not exercise the published updater at all.

**Repair:** Pin the exact public `0.19.2` wheel by version, URL, SHA-256, and
size. Require its installed updater to identify every manager accurately. Keep
the pipless-environment probe, but make `uv-pip` run the same real
`algo-cli update` entrypoint as the other POSIX managers. Remove the manual
upgrade bypass and report that no bootstrap is required. Add a regression test
that rejects a return to the bypass.

**Verification:** The focused upgrade-test module passed 40 tests and scoped
Ruff passed. Real isolated macOS upgrades from the pinned `0.19.2` wheel to the
locally built `0.20.0` wheel passed for `pip`, `pipx` with both `pip` and `uv`
backends, `uv tool`, and pipless `uv-pip`. Every run verified 329 predecessor
files and 333 candidate files against wheel bytes, preserved 11 seeded state
files plus SQLite integrity, and passed a repeat update. Final rebuilt artifact
digests, the full suite, hosted operating-system jobs, and publication remain
pending.

**Prevention and limits:** Every release must advance the predecessor fixture
to the live PyPI latest version and pin its exact immutable bytes. A passing
upgrade from an older package is not predecessor qualification, and a
test-owned package-manager command is not evidence that `algo-cli update`
works. Local macOS manager coverage does not substitute for hosted Windows and
Linux qualification or public post-publication install and upgrade checks.

## 2026-09-21 - Whole-Codebase Jev-Assisted Review (Completed; Repairs Pending)

**Component and scope:** Review the current `release/v0.20.0` working tree with
separate hardening, bug-hunt, quality-of-life, and quality-of-service passes.
The tree starts at `aed8e31b0d3be7358a25785a01f2fc7637261adf`, four commits
ahead of `origin/main`, with uncommitted Continuum integration changes. This is
a review checkpoint, not a repaired or release-qualified state.

**Observed evidence:** Shared and private Continuum verification, status,
and bounded context validation passed with no critical blockers or repair queue.
Jev's first hotspot-ranking request was rejected because repository paths were
invalid candidate IDs; a single corrected request used neutral IDs and ranked
release/workflow authority, installation, packaging, and the large runtime/tool
entry points highest for inspection. The focused Continuum tests passed 39
tests and repository Ruff passed. The first full run passed 6,224 tests with 41
skips and 26 failures: 24 exercised the still-required legacy Echo dependency in
an environment where it was absent, and two correctly rejected qualification
artifacts made stale by the dirty source tree. Mypy found a generated action-spec
risk-type mismatch and an existing Python 3.10 `tomli` import-ignore problem.

**Confirmed unresolved findings:** The maintainer confirmed that Echo Veil is retired and
must no longer be a selectable Algo memory backend. The tree still contains Echo
selection fields, runtime branches and tools in 23 production files, plus a
required dependency group, 36 test files, qualification scripts/workflows and
current documentation. Historical lessons may retain dated evidence, but active
configuration, runtime, dependency and qualification surfaces require migration
to Continuum or removal. This is not only dead-code cleanup: a temporary malformed
`config.json` reproduction made `Config.load()` set `echo_veil_enabled: true` and
`echo_veil_protection: required` while leaving Continuum disabled. Normal startup
then enters the Echo auxiliary preflight, so corrupt or malformed configuration
can still reproduce an Echo-branded startup stop after Echo retirement. The
fail-closed branch must preserve continuity safety without selecting a retired
backend. Jev supported this conclusion only after receiving the source trace and
executed reproduction. Separately, `_reconciliation_counts()` reads only the
first 128 lines of each accepted receipt file and does not mark that truncation
as incomplete. A 129-line local reproduction placed an unresolved memory
mutation on the final line; the function returned zero unresolved operations and
`receipt_scan_incomplete: false`. The new Continuum adapter also rewrites falsey
non-object `sources` and `depends_on` inputs with `value or {}`. An executed
direct-call probe supplied empty lists and observed empty objects at the backend
boundary, bypassing Continuum's canonical object-type rejection and weakening
provenance validation. The action-program compiler rejects that type mismatch,
but ordinary direct model tool dispatch does not enforce annotations. No repair
has been applied yet.

The Continuum cutover also does not inherit Irene's protected-root boundary.
`protected_tool_policy_error()` returns immediately unless Echo is selected;
the Continuum-specific preflight rejects several legacy memory actions but not
protected filesystem access, `run_shell`, or the unqualified Cobalt browser.
A synthetic owner-private `CONFIG_DIR` probe returned its canary through
`read_file` with Continuum selected, while the same probe was refused with Echo
selected. YOLO grants ordinary observations outside `cwd`, and the generic
sensitive-path list does not classify `.algo_cli`, so this contradicts the
documented claim that YOLO retains memory protections. Treat this as a
release-blocking hardening regression until the backend-independent protection
is restored and tested. Jev supported the finding after receiving the source
trace and executed probe; it did not independently discover it.

An end-to-end YOLO probe confirmed the same boundary loss through the ordinary
shell path: after a user-initiated `yolo` mode selection, preflight allowed
`run_shell`, the action was admitted without a prompt, and `cat` returned a
canary from a synthetic `.algo_cli/private-memory.txt` outside the workspace.
This is stronger evidence than the direct policy-function probe because it
exercised mode selection, grant preparation, preflight, approval, and command
execution together. The repair must therefore cover shell access as well as
file-tool path checks.

**Working lesson and remaining limits:** Jev ranking is an investigation order,
not a defect, severity, or cleanliness verdict. Keep exact paths in candidate
text and use bridge-safe neutral IDs. Every candidate finding still needs a
source trace, caller/test inspection, and a deterministic reproduction or
contract proof. Passing focused tests do not qualify the dirty tree, installed
runtime, hosted operating systems, or release. A retired dependency must not be
reinstalled merely to turn its obsolete contract green; first remove or replace
the active contract and keep only explicitly justified compatibility handling.
Bounded receipt scans must expose every omitted region as incomplete and must not
report a clean reconciliation count after silent truncation. Update this entry
with confirmed causes, verification, and any actual repairs instead of creating
duplicate entries for the same review.

For asynchronous and stateful behavior, build review packets around the complete
transition: initiating action, pending operation, shared state, completion or
failure handler, and displayed result. Run invariant-based adverse-order probes
even when Jev rejects a candidate. Record whether Jev found the candidate before
execution or only supported it after seeing the reproduction; the latter is
evidence interpretation, not independent discovery. Retain a sanitized packet
recipe with source hashes, exact ranges, question version, assumptions and probe
IDs so the judgment can be reproduced without credentials or private payloads.
The first post-reproduction Jev check supported the Continuum input finding; it
did not discover it independently. A separate malformed-provider-call probe
kept original, serialized, normalized and expected-result counts equal at four,
including duplicate-ID quarantine. Therefore the nearby `zip()` calls are not a
reported defect on current evidence; revisit only if a producer can mutate the
retained call list between those operations.

The core file tools exposed two separate bounded-work defects. `read_file()`
uses `Path.read_text()` before applying `max_chars`; a 32 MiB temporary-file
probe requested one returned character but reached about 67 MiB of traced peak
allocation. Its output is bounded, but its I/O and memory use are not, and a
non-default `start_line` adds another full-text split. `list_directory()` sorts
the complete iterator before slicing: a `limit=1` probe consumed all 25 test
entries. It also labels an exactly-full result as truncated and reports a
nonempty directory as empty for `limit=0`. Stream or incrementally scan bounded
file content, validate directory limits, inspect at most `limit + 1` entries,
and test both resource consumption and honest truncation state. Jev supported
the `read_file` conclusion after receiving the source and executed memory probe;
it did not discover the defect independently.

A Standard Codex Security scan was sealed as
`394a543a-78c9-4b5e-b95b-7da92c6361ed` against snapshot
`codex-security-snapshot/v1:sha256:53bebf5a84a6022484190379f5478b711ffe78bdbd24acb26f3a60c4d8d8b69e`.
It reported the Continuum/YOLO protected-memory regression at medium severity
and the file-inspection resource-exhaustion defect at low severity. Coverage was
explicitly partial: seven security surfaces were reviewed across a 791-file
inventory, but two independent security workers became nonresponsive and
returned no usable results. The parent review validated both findings with
source traces and bounded reproductions. Cobalt URL and tab-identifier handling
remains deferred because the receiving browser service is outside this
repository, so the client-side forwarding behavior alone does not establish a
security impact. The scan's working-tree-change warning was emitted while this
lessons entry was being updated; no product repair was applied during the
review.

Cross-language and delivery checks completed during this checkpoint: the
focused Python release, upgrade, packaging, authority, and daemon suites passed;
Go tests, race detection, 68.2% statement coverage, vet, and govulncheck passed;
Rust formatting, Clippy, seven tests, and RustSec audit passed; Austin passed 89
Swift tests; and the website passed its production build, eight Node tests,
ESLint, and a zero-vulnerability npm audit. An exact `0.20.0` sdist and wheel
built, and the isolated wheel smoke passed with 804 installed corpus records.
All five workflow YAML files parsed and all 130 external action references were
full-SHA pinned. These checks establish reviewed surfaces, not release
qualification for the dirty tree.

The installed `0.20.0` daemon is currently stopped. Its retained log records a
second clean SIGTERM shutdown on 2026-09-11 after the 2026-09-10 restart, with
bounded drain and no crash signature. Phase 1 intentionally has no launchd
registration or automatic restart, so this is a persistence/operations gap,
not a newly demonstrated daemon implementation failure. The signal sender is
still unknown; no daemon restart was performed during this review.

The Astra model-discovery repair is present in the current commit: the alias,
subscription-model set, response-transport capability handling, picker label,
and selection test all include `gpt-6-astra`. A live authenticated catalog call
during this review returned Astra plus Sol, Terra, Luna, and `gpt-5.5`. The
earlier missing-Astra symptom is therefore not reproduced in this checkout and
should not be carried as an open defect. The static fallback-list comment is
misleading because discovery failure intentionally returns no advertised
models, as an existing test requires; this is documentation debt unless product
policy changes to allow verified fallback selection.

### 2026-09-21: Native Continuum Local Repair

**Operational correction:** The working `.venv` was incorrectly assumed to be
test-only. A late launcher check proved `~/.local/bin/algo-cli` points into
`Algo-cli-yolo-mode/.venv/bin/algo-cli`, so the non-editable reinstall changed
normal launches too. The earlier test-only/no-live-change statements were wrong.
Resolve the normal launcher and interpreter before any environment sync.
The saved config initially selected the retired adapter and was explicitly
converted to native Continuum. At 15:59 CDT a later writer restored the retired
Echo/D-57 fields and removed `continuum_enabled`; the writer is not identified,
and no Algo process remained when the rewrite was diagnosed. Startup correctly
returned `memory_config_requires_repair` instead of interpreting those fields as
Continuum. The new explicit `algo-cli config memory repair` command validates a
closed JSON object, retains an exact mode-0600 digest-named backup, removes only
retired memory selectors/repair marker, and sets `continuum_enabled=true`. The
live repair retained
`~/.algo_cli/config.json.before-continuum-21b9946aa5285ed3.bak`; every unrelated
value matched before and after, no retired selector remains, and the repaired
config is mode 0600. A stale retired binary can still rewrite its old schema;
close old Algo sessions before repair and treat a repeat as a distinct writer
investigation rather than silently looping the migration.

The actual normal launcher then exited 1 during protected auxiliary preparation:
the goal ledger had schema 1 while schema 3 was required, and an existing trusted
goal-store anchor was at sequence 1. The runtime correctly refused the conflict.
No staged transaction existed and neither earlier recovery copy verified against
that anchor. The new explicit `algo-cli config memory repair-goal` path archives
the exact legacy bytes, preserves only the user-authored goal and cwd as blocked,
non-resumable state, strips untrusted progress/reason text, and publishes the
successor at anchor sequence 2. It never resets the anchor or silently resumes
work. The live archive is under
`~/.algo_cli/recovery-backups/goal-ledger-conflict-930dde63a7459101/`.
After both repairs, the actual PATH `algo-cli doctor` completed with exit 0.
Its `DEGRADED` result is limited to safe mode being off and auto-approval being
on; all memory preparation reached normal startup. No model request was made.

The first repair attempt incorrectly treated a retired adapter name as a
Continuum compatibility contract. The maintainer explicitly corrected that assumption:
Continuum is independent of D-57, and neither D-57 nor Echo Veil may be selected
or used as a fallback. Current source now calls the native `continuum-memory`
CLI directly with fixed project/harness identity and explicit scopes. Retired
keys are input-detection only: ambiguous or malformed configuration stops before
model execution and preserves the original file for repair. Historical stores
and historical reports are unchanged. This remains unpublished source work;
the normal-runtime activation problem is recorded above.

The initial four native-memory/path/preflight suites passed 91 tests. A later
full run found obsolete backend expectations and stale source-bound artifacts.
Protected search also refused every ordinary request because `python -I` loaded
an older non-editable package while the parent loaded the checkout. Installing
this checkout into its test-only `.venv` restored the existing search suite;
the root-identity check was not removed. Verify both normal and isolated Python
imports before diagnosing such failures as product defects.

The boundary investigation also exposed aggregate and nested routes: slash
commands with file/browser side effects, Git diffs over protected descendants,
parent-directory metadata, and same-inode filesystem aliases. New regressions
exercise those routes with synthetic canaries and ordinary-workspace controls.
Native receipt scanning now examines all rows in a bounded file, reporting
incomplete scans rather than silently ignoring unknown outcomes outside a tail.
The independent candidate review identified four surviving issues and new
tests reproduced them: `symlink/..` validation/execution disagreement, Git
hardlink aliases, replacement between text-read validation and open, and an
unrecognized retired-protection string falling through to plaintext. The repair
now inspects path components before normalization, validates Git metadata and
NUL-delimited file inventory (including whitespace names), pins read descriptors
and ancestry, and rejects ambiguous retired config without activating a backend.
The regression fixtures use synthetic canaries; native Git exercises the alias
case, and ordinary file/Git controls remain available. The scoped suites pass.

The next full run reached 6,195 passes and 40 skips with only two stale-report
failures. CI's complete mypy command passed 77 files; the three path/Git modules
also passed an explicit check. Exact test-package/source parity passed for 285
Python files with no missing or unexpected modules. A fresh isolated installed
process exposes all 15 native tools, no retired backend modules/config fields,
and successfully verifies/reads shared and Algo-private Continuum. Qualification
reports are being regenerated through real runners; exact-commit hosted tests
and public delivery remain separate gates. The release skill's outdated Echo
audit instruction was corrected as part of the prevention work.

The clean wheel installed successfully with 795 corpus records. The actual
published `0.19.2` updater then upgraded to that local candidate through uv-pip
without a pip module, preserved all 11 synthetic state files, and passed repeat
update/payload checks. This is a macOS local-wheel test, not a public candidate
release, production Keychain check or other-platform proof. The initial final
installed suite had 6,197 passes and one expected successor-binding failure:
M9 still referenced the old M8 bytes. Append actual new M8/Nathan ledger evidence,
then regenerate and verify M9 before the last full suite. Keep the existing
milestone statuses blocked; source-bound local evidence is not external-browser
qualification. The first coverage run reached 67.87%, above the 57% floor,
but its stale-M9 failure was not counted as a passing suite.

Final verification after rebinding passed: the complete installed Python suite
exited 0 with 67.87% combined line/branch coverage against the unchanged 57%
floor; all 152 focused qualification checks passed; fresh Go race tests, seven
Rust tests, 89 Swift tests, and the website build/eight tests passed. M8 retains
nine passing local metrics and five blocked external metrics; M9 verifies that
blocked state. No commit, push or tag was made. The late normal-launcher finding
and the two explicit, evidence-preserving repairs above supersede the test-only
assumption and the earlier statement that normal startup remained blocked.
The read-only service smoke is not a full live agent mutation workflow, and
clean-commit hosted macOS/Windows/Linux delivery, external Cobalt qualification
and the daemon persistence decision remain open.

Verification commands: `PYTHONDONTWRITEBYTECODE=1 ALGO_TEST_REQUIRE_RIPGREP=1
.venv/bin/pytest tests --cov --cov-branch --cov-fail-under=57`, the configured
CI mypy command, `ruff check algo_cli tests scripts`, `go test -race -count=1
./...`, `cargo test --manifest-path harness-indexer/Cargo.toml --locked`,
`swift test --package-path native/austin`, website `npm test`, and the isolated
wheel/uv-pip upgrade runners. Final coverage XML is retained at
`/tmp/algo-native-coverage-final.xml`; the upgrade receipt is
`/tmp/algo-native-upgrade-uv-pip.json`. Keep log filenames outside
`COVERAGE_FILE.*`: coverage's parallel-data cleanup removed the final run's
redirected `.log` under that prefix. Its exit status and final XML were observed;
the prior failed run's surviving console log was not relabeled as passing.

Jev checked three bounded native-transport claims against source: it supported
direct native dispatch, non-overridable scope, and revision/readback binding.
It correctly left the all-callers recovery claim unresolved because the supplied
packet did not contain every caller. The 2,932-input-token advisory call took
about 356 ms with estimated provider cost $0.000123144. These judgments are not
execution evidence. The prevention lesson is to verify the current backend's
native contract and operation-expansion boundaries, not infer architecture from
old labels or restore obsolete dependencies to satisfy obsolete tests.

## 2026-09-21: Required Continuum Context Outgrew The Startup Budget

**Issue and cause:** The installed launcher crashed while computing the startup
footer. A live native request returned `REFUSE_BUDGET`: required shared records
and receipt metadata needed 12,477 bytes, exceeding Algo's fixed 12,000-byte
request. Private context at 6,000 bytes passed. Verification/status alone did
not exercise packet construction, so the earlier doctor smoke missed this path.

**Repair:** Retry only a validated size refusal, once per scope, preserving the
query and all required records, with a 32,768-byte ceiling. Verify every accepted
packet natively. Footer accounting now reports unavailable context without
crashing the REPL; actual system-prompt assembly still fails closed. No store,
trusted head, scope, record requirement, or fallback policy was changed.

**Verification:** Live source `prompt_context(Config.load(), ...)` retrieved and
validated shared context at 14,336 bytes and private context at 6,000 bytes.
`tests/test_continuum_context_budget.py` covers exact retry size, malformed and
oversized reports, bounded retry, verification refusal, UI recovery, and prompt
blocking. The surrounding Continuum, context, footer, command-discovery, Jev,
configuration, main-helper, tools and action-registry tests passed locally.
Installed-launcher qualification is recorded below when completed; these results
alone are not a public release or hosted-platform qualification.

**Prevention:** Test the actual context operation, not only backend health. Keep
required-state growth explicit and bounded; a display failure must not authorize
a model call without required memory. Jev's bounded-retry/preservation review was
advisory (736 input tokens, 350 ms); executable refusal tests are the evidence.

## 2026-09-21: Compact Command Discovery And Stable Footer Styles

**Issue and cause:** Completion and help rendered the entire slash registry,
including child commands and aliases. During generation a separate plain-text
footer renderer discarded the prompt's colors/styles. Rich's full-height live
answer renderer also did not account for the reserved footer row.

**Repair:** Keep runtime command compatibility, but show 12 common commands at
`/`, canonical roots while typing, and children only after the parent plus a
space. Default help shows common commands and eight categories; category/exact
command help and `/help all` retain the full reference. Render generation footer
content with the same prompt-toolkit theme/default styles and color-depth policy;
size Rich live output to the available rows. Preserve non-TTY behavior.

**Verification and limits:** The 79 command-discovery/footer/dispatch/display
tests passed. Additional tools/action-registry tests passed in the broader run.
Tests cover replacement offsets, aliases, every root's category/discoverability,
background/bold/foreground styles, wide-character clipping, and long live answer
height/completion. The reported repeated answer lines have a plausible viewport
cause, but the original provider stream was unavailable; do not claim that trace
proved a provider or rendering root cause. Jev reviewed discovery compatibility
advisorially (631 input tokens, 458 ms); it did not replace execution tests.

## 2026-09-21: Personal Catalog And Kernels Shipped In Public Packages

**Symptom and cause:** The maintainer's populated `docs/ALGO.md` catalog was
force-included in the wheel and sdist, and business-specific finance,
construction, and Acrobat modules were part of the `algo_cli` package and
kernel manifest. Nothing distinguished framework code from one user's library.

**Repair:** `docs/ALGO.md` is now an empty template. The harness indexes
`<config>/ALGO.md` when present; `/intelligence init` creates it from the
template. Personal kernels load from `<config>/kernels/kernels.json`, appended
to `sys.path` and unable to replace built-in names. The removed modules were
preserved locally with checksums before deletion. `check_public_release.py`
rejects any populated `ALGO.md` and personal-library paths in the tree and in
built artifacts; `check_public_history.py` exempts only already-published
history, per the owner's decision not to rewrite it.

**Verification and limits:** 6281 installed-suite tests passed with 40 skipped;
catalog-content tests read the developer's own catalog and skip in CI. Built
wheel and sdist contain only the 1.7 KB template. Earlier tags, public Git
history, and the published 0.19.2 files still contain the old catalog and
modules; this repair does not retract them.

## 2026-09-22: Chrome Pin Went Stale Between Local Qualification And Merge

**Symptom and cause:** The `main` CI run for the v0.20.0 merge failed the
Boron public-browser job with `hosted_browser_security_update_stale`. The pin
was `153.0.8010.36` (served 2026-09-08); Chrome had shipped `.47` and `.52`
since, and the 72-hour lag gate is measured against the newest stable release.
Nothing in the candidate touched the browser.

**Repair:** Pinned `153.0.8010.52` (VersionHistory serving start
`2026-09-18T00:49:42.244859Z`, release-at-ms `1789692582244`, deb sha256
`29e0e4b5af01213915ffdb7f4e49a11956dc5fc591165c85af8688dcdff907ac`).
Regenerated the dpkg lock by building the pinned stage through the
`dpkg-query` step on linux/amd64: 228 entries, sha256
`8b33f574a394cc249f5546a8f3c089aea2ab0d1c372d190acc74c7443381f52d`.
Substituting the old Chrome version into the new lock reproduces the old
digest exactly, so Chrome is the only changed package. The newer-but-stale
fixture moved to `.53` and the hosted observed-at fixture past the new release.

**Verification and limits:** 551 Boron tests passed locally. This refreshes
only the version pin; the hosted job still has to observe the live feed and
build the image. Because releases arrive roughly weekly, tag, dispatch, and
publication should follow a pin refresh within about three days.

## 2026-09-22: Public Website Described A Three-Release-Old Version

**Symptom and cause:** After v0.20.0 was published and verified, algo-cli.com
still advertised v0.17.0 on the install page, home badge, footer, docs index,
and the machine-readable release manifest. The site is deployed manually from
`website/` and had not been part of the last three release checklists.

**Repair:** Pointed every version surface at v0.20.0 with the immutable tag,
source revision `55a7ec5`, and the public wheel/sdist digests; added install
cards for in-place upgrade, personal catalog/kernel setup, Continuum
selection and `/memory doctor`, and optional Jev setup. Review corrected two
overclaims before merge: `config memory status` reports keyring anchors, not
Continuum selection, and a 0.19.1 install in a pip-free `uv pip` environment
cannot self-update. Benchmark data stays labeled with the v0.17.0 revision it
was measured on.

**Verification and limits:** `npm test` passed 8/8 after a fresh build and the
rendered `/install` contains no 0.17.0 reference. That is local build state
only; the public site changes when `npm run deploy:cloudflare` is run and its
result is observed. Prevention: the release checklist should include the
website manifest and a rendered-page check before publication is called
complete.

## Repair Log Checklist

- Date and component.
- Observed symptom and confirmed cause; identify anything still unknown.
- Exact repair and its local, deployed, or operational scope.
- Verification commands, results, and evidence links.
- Prevention or regression coverage, plus remaining limits.

Never include credentials, personal state, raw sensitive logs, or unsupported
success claims. A failed check remains a failed check even after later recovery.
