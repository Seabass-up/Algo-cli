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

## 2026-09-23: Schemeless Ollama Host Failed Readiness

**Symptom and cause:** theodore_runtime_services.py:191 - ollama_server_ready fails for a host without a scheme (OLLAMA_HOST=127.0.0.1:11434), so the REPL runs a second `ollama serve` and skips every prompt

**Repair:** Added normalize_ollama_host(), which adds http:// when the scheme is missing. It is used in the ollama_server_ready /api/version probe and in start_supplemental_gateway's local_service_address check. Hosts that already have a scheme behave as before.

**Verification and limits:** tests/test_providers_fixes.py::test_ollama_server_ready_accepts_host_without_scheme (127.0.0.1:PORT, localhost:PORT and http:// probed against a real local HTTPServer) and ::test_start_local_ollama_host_does_not_spawn_for_schemeless_running_host. Both fail before the fix and pass after it.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Normalize user-supplied hosts at every probe and launch boundary, and test the documented schemeless form.

## 2026-09-23: Gateway Received A Schemeless Ollama Host

**Symptom and cause:** algo_cli/theodore_runtime_services.py:328 - the gateway was launched with the raw cfg.host, which has no http:// scheme, so it exited; Python then waited 20 to 45 s before reporting 'did not become ready'

**Repair:** start_supplemental_gateway now normalizes the host once (ollama_host = normalize_ollama_host(cfg.host)). It uses that value for both the loopback check and the gateway's `-ollama` argument, so the Go validateOllamaHost check gets a full http:// URL. The wait loop now checks GATEWAY_PROCESS.poll() on each pass. If the process has exited, it clears GATEWAY_PROCESS, reports 'Supplemental gateway exited with code N before becoming ready at URL' and returns False immediately.

**Verification and limits:** New test_supplemental_gateway_gets_normalized_host_and_fails_fast_on_exit: with host '127.0.0.1:11434' and a fake Popen whose process has already exited, the -ollama argument is 'http://127.0.0.1:11434'. The function returns False without sleeping (sleep is patched to fail the test), GATEWAY_PROCESS is None, and the error names the exit code.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Pass one normalized host to every child process, and check child exit while waiting for readiness.

## 2026-09-23: Agent Blocks Routed By The Active Model

**Symptom and cause:** theodore_runtime_services.py:88 - client_for_model picks Ollama Cloud or local from the active model's route instead of the agent block's model

**Repair:** model_routing.uses_ollama_cloud(cfg, model=None) now takes an optional model. For a block model that is not the active model, it returns False for xAI and ChatGPT models. Otherwise it returns cfg.cloud and is_cloud_model_name(model) and OLLAMA_API_KEY. Active-model behaviour is unchanged. client_for_model uses uses_ollama_cloud(cfg, model). The redundant require_cloud_api_key call there was dropped because the key is already required by the route check.

**Verification and limits:** tests/test_providers_fixes.py::test_cloud_block_routes_to_ollama_cloud_while_xai_model_is_active, ::test_local_block_routes_to_local_host_while_ollama_cloud_is_active and ::test_uses_ollama_cloud_for_active_model_is_unchanged. All fail before and pass after.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Route by the model that is actually being called, and test a block model that differs from the active one.

## 2026-09-23: Plain-Named Cloud Block Models Went To The Local Host

**Symptom and cause:** algo_cli/model_routing.py:81 - an agent-block model with a plain name (e.g. qwen3-coder:480b) went to the local cfg.host in direct Ollama Cloud mode

**Repair:** For a block model that is not the active model and not xAI or ChatGPT, uses_ollama_cloud now uses the same test as the active model's Ollama route: cfg.cloud and OLLAMA_API_KEY. It no longer requires the name to end in :cloud or -cloud. An Ollama block still goes to Ollama Cloud while an xAI or ChatGPT model is active, which was the original purpose of the model parameter. The docstring now explains why the name is not checked.

**Verification and limits:** Replaced test_local_block_routes_to_local_host_while_ollama_cloud_is_active, which assumed a plain name means local; that assumption is wrong in direct cloud mode. New test_unsuffixed_block_routes_to_ollama_cloud_in_direct_cloud_mode: Config(model='gpt-oss:120b', cloud=True), block model 'qwen3-coder:480b' goes to https://ollama.com and gets the same route as the active model. New test_cloud_suffixed_block_uses_local_daemon_when_direct_cloud_is_off: with cfg.cloud=False, a -cloud block uses the local daemon. Kept test_cloud_block_routes_to_ollama_cloud_while_xai_model_is_active.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Keep block routing identical to active-model routing unless a test names the difference.

## 2026-09-23: Env-File xAI Key Silently Overrode The Shell

**Symptom and cause:** xai_auth.py:94 - with load_runtime_env(override=True), a stale XAI_API_KEY in ~/.algo_cli/env silently overrides a key exported in the shell

**Repair:** I took the spec's alternative fix, because the precedence is intentional and repo-wide: main.py:4959 and 20+ other call sites in files I do not own also use override=True, including at startup. Changing the default in xai_auth alone would not stop the file value from overriding the shell. auth_status() now returns api_key_source ('runtime_env_file' | 'environment' | None) without exposing the key. The require_api_key error now says a value saved in the Algo CLI env file takes precedence. The exact-dict assertion in tests/test_xai_auth.py was updated for the new field.

**Verification and limits:** tests/test_providers_fixes.py::test_xai_status_reports_env_file_key_precedence, ::test_xai_status_reports_environment_key_source and ::test_require_api_key_explains_env_file_precedence. All fail before and pass after.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Report which source supplied a credential whenever precedence can surprise the user.

## 2026-09-23: Tool Arguments Were Parsed As Rich Markup

**Symptom and cause:** display.py:791 show_tool_call used raw tool args inside Rich markup, so 'ls [/tmp]' raised MarkupError after the assistant tool_calls message was already stored, and 'd[key]' was silently cut

**Repair:** show_tool_call now builds the line with Text.assemble. The glyph, name and key are styled and each value is appended as plain text. The JSON sink path is unchanged.

**Verification and limits:** tests/test_display_fixes.py::test_show_tool_call_keeps_bracket_arguments_literal (fails on HEAD with MarkupError, passes now). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Never interpolate tool, model or file text into Rich markup; build Text objects.

## 2026-09-23: Tool Output Previews Dropped Bracketed Text

**Symptom and cause:** display.py:815 show_tool_result put each preview line inside '[muted]{line}[/]' markup, so 'd[key]' showed as 'd' and '[/tmp]' crashed the turn

**Repair:** The header and preview lines are now Text objects (style muted / success / error / bold) instead of markup f-strings

**Verification and limits:** tests/test_display_fixes.py::test_show_tool_result_preview_keeps_brackets. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Test display helpers with bracketed code such as d[key] and closing-tag lookalikes.

## 2026-09-23: Literal Markup Tags In The Thinking Panel

**Symptom and cause:** UI proposal u6: thinking panel that follows the tail (also fixes an existing bug where the literal text '[muted]... truncated[/]' appeared, because the markup sat inside a Text)

**Repair:** While streaming, _thinking_renderable shows the last 1200 characters under a muted '... N earlier chars' note. The final panel keeps the opening text plus a muted '... truncated, ~N tokens total' note, both built as styled Text.

**Verification and limits:** tests/test_display_fixes.py::test_live_thinking_panel_follows_tail, ::test_final_thinking_panel_truncation_note_is_styled_not_markup (existing test_display thinking tests still pass). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Text() never parses markup; use styled Text segments instead of tags inside Text.

## 2026-09-23: Code-RAG Failed After An Embedding Width Change

**Symptom and cause:** code_rag.py:781: Code-RAG embeddings were keyed only by model name, so a change in embedding width made retrieve() raise a numpy matmul ValueError on every turn

**Repair:** ensure_embeddings() now takes a dimensions= argument and counts a chunk as pending when the model, the vector width or the embedder identity differs (the new _embedding_matches helper, which works like harness._embedding_matches). Each chunk now stores embedding_dimensions and embedding_identity. retrieve() resolves the identity once per turn, because a bound embedder's check probes the provider. It then embeds the query, calls a shared _ensure_embeddings with the query's width, and keeps only candidates of that width and identity; if the identity is unavailable it returns []. _reuse_content_embeddings copies the width and identity fields as well. The one-time user notice in main.py was NOT done because I don't own main.py.

**Verification and limits:** tests/test_rag_fixes.py::test_code_rag_reembeds_after_embedding_width_change (before the fix: ValueError 'size 4 is different from 8'), test_code_rag_reembeds_when_embedding_identity_changes (before the fix: KeyError embedding_identity). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Bind stored vectors to model, width and embedder identity, not model name alone.

## 2026-09-23: Legacy Code-RAG Chunks Vanished After Upgrade

**Symptom and cause:** algo_cli/code_rag.py:890. After an upgrade, retrieve() drops chunks from older indexes that have no embedding_identity, so a bound embedder only sees the chunks re-embedded so far (at most EMBED_PER_TURN_CAP per turn)

**Repair:** Added a keyword-only allow_legacy flag to _embedding_matches. When it is set, a chunk with no 'embedding_identity' key still counts as a match if its model and vector width match. retrieve() passes allow_legacy=True when it picks candidates, so the whole legacy index stays searchable during migration. _ensure_embeddings still uses the strict match, so legacy chunks keep being re-embedded and tagged in the background, EMBED_PER_TURN_CAP per turn. Chunks tagged with a different identity are still excluded, so the protection against an identity change is kept.

**Verification and limits:** Added tests/test_rag_fixes.py::test_code_rag_keeps_legacy_unbound_chunks_retrievable_during_identity_migration. It builds 10 chunks with an unbound embedder, sets the cap to 3 and runs a bound embedder (sha256 identity). It checks that all 10 chunks come back as hits and exactly 3 get the identity. It then checks that chunks tagged with a different identity return nothing. The test fails when retrieve() uses allow_legacy=False and passes with the fix.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Accept unbound legacy records that still match model and width during migration.

## 2026-09-23: Deleted Files Stayed In The Code-RAG Repo Map

**Symptom and cause:** code_rag.py:747: when the only change was a deleted file, the old project graph was reused, so the repo map still ranked the deleted file

**Repair:** build_or_update_index now reuses prior_structural only when every file was reused and new_files.keys() == old_files.keys()

**Verification and limits:** tests/test_rag_fixes.py::test_code_rag_repo_map_drops_deleted_file (before the fix: b.py was still in the structural snapshot). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Reuse a derived graph only when the file set is unchanged, including deletions.

## 2026-09-23: Future-Dated Sources Forced Endless Harness Rebuilds

**Symptom and cause:** harness.py:1511: a source file dated in the future made the harness index stale permanently, so every load rebuilt and rewrote it

**Repair:** Added _record_signature_matches(record, stat). _source_watermark_ns now skips an indexed record's path when its current size and mtime_ns match the record's stored file_size and file_mtime_ns, because that file is already in the index. Changed or new files still raise the watermark. This also covers the source-change guard in embed_index_records.

**Verification and limits:** tests/test_rag_fixes.py::test_future_dated_indexed_source_does_not_keep_index_stale (before the fix: index_is_stale() was True straight after a matching index; it also checks that an edit to a future-dated file is still detected). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Compare stored size and mtime signatures instead of a global newest-mtime watermark.

## 2026-09-23: Tool Failures Were Labeled As Worked

**Symptom and cause:** nathan_runtime.py:1195 classify_tool_status labels 'Error <verb>ing ...' tool failures (Error writing/reading/listing/searching/running/fetching/searching web, etc.) as 'worked'

**Repair:** Added _TOOL_ERROR_PREFIX_RE, which matches 'error (reading|writing|listing|searching|running|fetching|extracting|rendering|generating|executing|pulling|deleting|creating|copying|showing) ...: ', and checked it right after the existing 'error:' prefix check. The verb list stays tight so file content such as 'Error handling ...' or 'error rates ...' is still classified 'worked'.

**Verification and limits:** tests/test_tools_fixes.py::test_classify_tool_status_marks_verb_prefixed_tool_errors_failed (9 cases, all fail on HEAD) plus test_classify_tool_status_keeps_ordinary_error_text_worked (guard cases). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Classify tool results by the exact error prefixes tools emit, with a test per prefix.

## 2026-09-23: Error-Like File Content Was Labeled As A Failure

**Symptom and cause:** nathan_runtime.py:1202 _TOOL_ERROR_PREFIX_RE marks raw read_file content and exit-0 run_shell output starting 'Error <verb>ing ...: ' as failed

**Repair:** The regex now requires an absolute path target (/, X:\ or \\) for 'reading|writing|listing'. Every built-in file tool reports the resolved absolute path, so a line like 'Error reading sensor 3: timeout' is no longer treated as a tool error. classify_tool_status now computes the [exit code: N] matches first and skips the prefix check whenever an exit code is present, so shell output is judged only by its exit code. Genuine errors such as 'Error reading /x: ...', 'Error reading C:\x.txt: ...', 'Error running command: ...' and 'Error searching: ...' are still classified as failed.

**Verification and limits:** tests/test_tools_fixes.py::test_classify_tool_status_keeps_raw_error_like_output_worked covers three cases: read_file multi-line content, unnamed single-line content, and run_shell with exit code 0. tests/test_tools_fixes.py::test_classify_tool_status_uses_exit_code_for_error_like_shell_output checks that exit code 1 still gives failed and that a Windows-path read error gives failed. The existing verb-prefixed failure tests still pass.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Anchor failure detection to tool-emitted shapes (absolute path targets), not any leading 'Error'.

## 2026-09-23: Partial ripgrep Errors Discarded All Matches

**Symptom and cause:** tools.py:1531 / search_execution.py:141 search_files discards every rg match when rg exits 2 on a partial error such as an unreadable subfolder

**Repair:** When rg exits with a code above 1 and stdout has matches, search_files now returns the matches followed by '[partial: search reported N error(s); first: <first stderr line>]'. 'Error searching:' is kept for a nonzero exit with no stdout. A stderr overflow alone no longer turns the result into an error.

**Verification and limits:** tests/test_tools_fixes.py::test_search_files_keeps_rg_matches_when_rg_reports_partial_errors (stubbed) and ::test_search_files_real_rg_with_unreadable_subfolder_returns_matches (real rg with a chmod 000 folder, skipped on Windows, as root or without rg). Both fail on HEAD.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Treat ripgrep exit 2 with stdout as partial success and say so.

## 2026-09-23: Large stderr Stopped Searches Early

**Symptom and cause:** search_execution.py:141 a 4 KiB stderr cap ends and kills the search, so many permission errors stop it early

**Repair:** _Capture gained drain, drain_limit (1 MiB) and discarded fields plus a stopped property. The stderr capture keeps reading and discarding output past its 4 KiB cap, and the wait loop breaks only on stopped or error. The stdout cap and the deadline still end the search, and an unbounded stderr writer still stops at the 1 MiB drain limit, so test_process_capture_stops_an_unbounded_writer[stderr] still passes.

**Verification and limits:** tests/test_tools_fixes.py::test_run_search_process_drains_large_stderr_without_ending_search (about 105 KB of stderr, then a stdout match; fails on HEAD). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Cap what is kept, not what is read; drain pipes past the retention cap.

## 2026-09-23: Python Search Fallback Ignored Path Globs

**Symptom and cause:** search_execution.py:189 the Python fallback matches globs against the file name only, so '**/*.py', 'dir/*.py' and '!*.md' return 'No matches.'

**Repair:** Removed fnmatch and added a stdlib glob_matcher/_glob_regex that follows ripgrep's rules: a glob without '/' matches the file name, a glob with '/' matches the path relative to the search root, a leading '!' negates, and '**/', '**', '*', '?', [..] and {a,b} are supported. This works under the child's -I -S flags, where wcmatch is unavailable.

**Verification and limits:** tests/test_tools_fixes.py::test_search_files_python_fallback_honors_rg_style_globs (**/*.py, pkg/*.py, !*.md, *.{py,txt} with rg absent) and ::test_glob_matcher_matches_ripgrep_semantics. Both fail on HEAD.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Match fallback globs with ripgrep's rules and test the fallback separately from rg.

## 2026-09-23: Windows Fallback Globs Became Case-Sensitive

**Symptom and cause:** search_execution.py:235 glob_matcher became case-sensitive on Windows, which narrows the rg-less fallback compared with the old fnmatch/normcase behaviour

**Repair:** glob_matcher now compiles with re.IGNORECASE when os.name == 'nt'. This restores the earlier Windows case-insensitive behaviour. POSIX stays case-sensitive, matching rg.

**Verification and limits:** tests/test_tools_fixes.py::test_glob_matcher_case_sensitivity_follows_platform monkeypatches os.name. On 'nt', '*.py' matches 'SETUP.PY' and 'docs/*.md' matches 'DOCS/README.MD'. On 'posix', '*.py' does not match 'SETUP.PY'.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Preserve platform filesystem case rules when replacing a matcher.

## 2026-09-23: run_shell Stripped Significant Leading Whitespace

**Symptom and cause:** tools.py:1651 run_shell .strip() removes significant leading whitespace from the first output line (git status --short ' M' becomes 'M ')

**Repair:** stdout and stderr now have only leading newlines and trailing whitespace removed (.lstrip('\r\n').rstrip()), so leading spaces are kept.

**Verification and limits:** tests/test_tools_fixes.py::test_run_shell_preserves_leading_whitespace_of_first_line (POSIX only; fails on HEAD). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Trim only framing newlines and trailing space from command output.

## 2026-09-23: One-Shot Mode Could End Without A done Event

**Symptom and cause:** oliver_oneshot.py:444 cfg.save() in finally raises (memory config needs repair) so no 'done' event is written

**Repair:** Wrapped cfg.save() in the finally block in try/except. A failure now sends sink.error('Config save failed: ...'), so the run reports status partial (or stays failed) and sink.done() is always written with exit code 2.

**Verification and limits:** tests/test_pipeline_fixes.py::test_oneshot_emits_done_when_config_save_refuses. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Terminal protocol events belong outside anything in a finally block that can raise.

## 2026-09-23: Failed Agent Blocks Stayed In Running State

**Symptom and cause:** agent_pipeline.py:681 a model stream exception leaves block.status 'running'

**Repair:** The except Exception branch around block_client.chat now sets status='failed', status_code='model_error' and status_reason='<Type>: <msg>' before re-raising. The completion panel and thread records then show a failed block.

**Verification and limits:** tests/test_pipeline_fixes.py::test_run_agent_block_marks_model_stream_failure_as_failed. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Set a terminal status before re-raising from any block execution path.

## 2026-09-23: Ordinary /agent Tasks Were Taken As Thread Commands

**Symptom and cause:** agent_pipeline.py:3207 tasks starting with show/switch/resume/fork are hijacked as thread commands; error shows stray quotes

**Repair:** A thread command now needs a hex thread ref (4-64 chars, the same format as the uuid-hex ids). show/switch also need that ref to be the only token. Anything else runs as a normal /agent task. KeyError messages are built from exc.args[0] through _key_error_message.

**Verification and limits:** tests/test_pipeline_fixes.py::test_plain_language_task_starting_with_thread_verb_runs_as_task[show|switch|resume|fork], ::test_unknown_thread_error_has_no_stray_quotes. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Require an explicit reference before treating a verb-led sentence as a command.

## 2026-09-23: Short Thread References Stopped Resolving

**Symptom and cause:** agent_pipeline.py:3253 thread commands required a 4-64 char hex ref, so short prefixes (show ab, resume a1b, fork 9f fix it) and mistyped refs (resume abcx) started a full pipeline

**Repair:** _THREAD_REF_RE now accepts 1-64 hex chars. is_thread_command is true when the argument is a single token for any verb, so a lone ref, even a mistyped non-hex one, goes to resolve_thread and gets 'Unknown agent thread'. It is also true for resume/fork when a hex ref is followed by task text. Multi-word plain-language tasks whose first word is not hex (for example 'show the failing tests') still run as tasks.

**Verification and limits:** tests/test_pipeline_fixes.py::test_short_and_mistyped_thread_refs_resolve_instead_of_running_task, parametrized over show ab / switch a1 / resume a1b / fork 9f fix it / resume abcx. It asserts resolve_thread got the ref, the error is 'Unknown agent thread' and no pipeline started. The existing plain-language parametrized test still passes.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Keep command detection as permissive as the resolver it feeds.

## 2026-09-23: Apostrophes Dropped /agent Options

**Symptom and cause:** agent_pipeline.py:1175 shlex parsing: an apostrophe drops --pipeline, quotes are stripped, and team fails with 'No closing quotation'

**Repair:** parse_agent_invocation_checked and parse_agent_team_invocation now read only the leading --pipeline/--roles option tokens (a regex tokenizer that also handles quoted values). The task body is kept raw, with outer quotes removed only when the whole task is one quoted string, so existing tests still pass. resume/fork also stopped using shlex: --same-worktree is removed by regex. The unused shlex import was dropped.

**Verification and limits:** tests/test_pipeline_fixes.py::test_pipeline_flag_survives_apostrophes_and_keeps_inner_quotes, ::test_team_task_with_apostrophe_is_accepted, ::test_resume_task_with_apostrophe_is_accepted. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Do not shell-tokenize free text; parse only leading option tokens.

## 2026-09-23: Trailing --roles Options Were Ignored

**Symptom and cause:** agent_pipeline.py:1162 parse_agent_team_invocation dropped trailing or mid-task --roles options (they stayed in the task text and default roles were used)

**Repair:** After the leading-option loop, _TRAILING_ROLES_RE takes --roles VALUE / --roles=VALUE (quoted or bare) from anywhere in the rest of the text and removes it, keeping the last one found. A --roles left with no value is a usage error (_BARE_ROLES_RE). The task text keeps its apostrophes and quotes.

**Verification and limits:** tests/test_pipeline_fixes.py::test_team_roles_option_is_parsed_anywhere (trailing, trailing with =, and mid-task) and ::test_team_trailing_roles_without_value_is_usage_error.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Test options in every documented position after changing a parser.

## 2026-09-23: Team Cancellation Waited For Every Specialist

**Symptom and cause:** agent_pipeline.py:2924 Ctrl+C in /agent team blocks until running specialists finish, and they can overwrite the cancelled records

**Repair:** The ThreadPoolExecutor is now managed by hand. On an interrupt it sets a shared threading.Event, attached to each member_cfg as _algo_team_cancellation, and calls shutdown(wait=False, cancel_futures=True). run_agent_block checks the event at every model round and every stream chunk (_raise_if_team_cancelled). _run_contract_bound_specialist checks it again after the block and before persisting terminal thread records, so a running specialist stops and does not overwrite the 'cancelled' records. Remaining gap: a specialist blocked in a model call only notices the event at its next chunk or round, and there is a small window if it is already inside a record write.

**Verification and limits:** tests/test_pipeline_fixes.py::test_team_interrupt_returns_without_waiting_for_running_specialists. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Give cooperative workers a cancellation signal and test cancel latency.

## 2026-09-23: A Helper Displaced The delegated_scope Decorator

**Symptom and cause:** agent_pipeline.py:242 @delegated_scope() moved onto _raise_if_team_cancelled, so run_agent_block no longer ran in the delegated scope (children, including team specialists on ThreadPoolExecutor workers, could inherit parent YOLO)

**Repair:** Put @delegated_scope() back directly above def run_agent_block. _raise_if_team_cancelled is now undecorated.

**Verification and limits:** tests/test_pipeline_fixes.py::test_run_agent_block_runs_in_delegated_scope_on_worker_threads calls run_agent_block on a ThreadPoolExecutor worker, spies on tool_policy.compute_policy and checks that session_mode._DELEGATED is True inside the call and False afterwards.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Keep decorators attached to the function they wrap when inserting helpers; test the scope on worker threads.

## 2026-09-23: Unreadable memory.json Could Be Overwritten With One Fact

**Symptom and cause:** config.py:2098 (high): an unreadable memory.json (0664, symlink, hardlink, corrupt or non-list) loaded as [], so the next /remember, /forget or reconcile replaced every stored fact

**Repair:** Added config._load_memory_facts_for_update(), which uses a sentinel default and raises the new MemoryFileUnreadableError(OSError) when the file exists but can't be used, so the write never happens. Config.remember_fact, reconcile_memory_facts and forget_memory_index now use it. Config.load sets a new non-persisted field, memory_load_error ('unreadable' or 'not_a_list'), and save() drops it. In julia_memory_runtime, _latest_legacy_facts, _remove_legacy_fact and remember_fact turn the error into MemorySystemError, so /remember and the remember tool show a clean error, and the catalog insert is rolled back or never made. /memory home now starts with a WARNING line, and /memory doctor reports ready=false plus memory_file_error.

**Verification and limits:** tests/test_memory_fixes.py: test_corrupt_memory_file_blocks_remember_instead_of_wiping_it, test_group_writable_memory_file_is_reported_and_preserved (POSIX only), test_non_list_memory_file_is_not_replaced, test_memory_home_and_doctor_warn_when_memory_file_is_unreadable, test_memory_load_error_is_not_persisted_to_config, test_missing_memory_file_still_accepts_first_fact. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Refuse writes when the prior state cannot be read; never treat unreadable as empty.

## 2026-09-23: Blank memory.json Was Treated As Unreadable

**Symptom and cause:** config.py:2112 a zero-byte memory.json is treated as unreadable

**Repair:** Added _memory_file_is_blank(). It reads through the same guarded _state_descriptor_payload, and an unsafe file returns False. A blank or whitespace-only memory.json now counts as an empty list in _load_memory_facts_for_update and does not set memory_load_error in Config.load. Unsafe, symlinked, corrupt or non-list files still fail closed.

**Verification and limits:** tests/test_memory_fixes.py::test_blank_memory_file_is_treated_as_empty_list[empty|whitespace]. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Distinguish empty from unreadable explicitly in state loaders.

## 2026-09-23: Memory Load Warning Persisted After Repair

**Symptom and cause:** julia_memory_runtime.py:1688 memory_load_error is never cleared after repair

**Repair:** memory_load_error is reset to '' after each successful locked load-and-write: Config.remember_fact, reconcile_memory_facts and forget_memory_index in config.py, and _remove_legacy_fact in julia_memory_runtime.py

**Verification and limits:** tests/test_memory_fixes.py::test_repaired_memory_file_clears_load_warning_after_successful_write. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Clear error state after the next successful locked load-and-write.

## 2026-09-23: One Invalid Legacy Fact Broke /remember And Recall

**Symptom and cause:** julia_memory_runtime.py:709 (medium): one legacy fact that is too long or contains a control character broke every /remember and turned off recall

**Repair:** sync_legacy_facts now catches MemorySystemError per fact, skips the bad one and returns a 'skipped' count. The legacy list itself is left as is. doctor() gains invalid_legacy_facts, and home_text (now built from a list of lines) shows 'Skipped N stored fact(s)...' with cleanup guidance.

**Verification and limits:** tests/test_memory_fixes.py: test_invalid_legacy_fact_does_not_block_remember[oversized|control-char], test_sync_legacy_facts_counts_skipped_and_doctor_reports_them. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Validate imported records individually and report skips instead of failing the batch.

## 2026-09-23: Paragraph Lessons Produced No Index Chunks

**Symptom and cause:** identity.py:282 (medium): the lessons chunker split only on '## ', so paragraph lessons gave 0 chunks, and short /lesson entries under 30 characters were dropped

**Repair:** _chunk_lessons now splits text before the first heading on blank lines, drops '# ' title lines and HTML comments, and keeps a headed section whenever its body is non-empty. A heading-only section still uses LESSON_MIN_CHARS. I did not follow the fix sketch's 'measure LESSON_MIN_CHARS on the body': the 'Use uv' body is only 6 characters, so that would still drop it.

**Verification and limits:** tests/test_memory_fixes.py: test_paragraph_lessons_without_headings_are_chunked, test_short_appended_lesson_is_kept, test_template_only_lessons_file_has_no_chunks, test_paragraph_lessons_reach_retrieval_index. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Chunk the documented template format, not only the heading form.

## 2026-09-23: Complaints Were Captured As Standing Rules

**Symptom and cause:** julia_memory_candidates.py:75 (medium): complaints and reported speech such as 'You always forget...' and 'I never said...' were accepted as standing-rule memories

**Repair:** Added _NON_DIRECTIVE_STANDING_RE. It rejects I/we/you + (should) always/never + a past-tense verb (with exceptions for need, proceed, succeed and similar words) or a reporting verb, and 'you always/never forget/ignore/miss/skip/break/fail/overlook/read...'. evaluate_candidate returns reason 'not_directive' for standing_rule candidates that match. Directive forms stay eligible, e.g. 'We never use pip directly', 'Always run...', 'You should always ask...' and 'We always need...'.

**Verification and limits:** tests/test_memory_fixes.py: test_non_directive_always_never_sentences_are_not_eligible (4 cases), test_directive_standing_rules_stay_eligible (4 guard cases). Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Reject past-tense complaints and reported speech before admitting durable rules.

## 2026-09-23: Valid Directives Were Rejected As Complaints

**Symptom and cause:** julia_memory_candidates.py:85 regex rejects 'should always tell/say' and 'red-team' directives

**Repair:** _NON_DIRECTIVE_STANDING_RE no longer accepts an optional 'should' (a 'should' sentence is always an instruction). Added (?!-) after the verb group so hyphenated words such as 'red-team' do not match the [a-z]+ed branch.

**Verification and limits:** tests/test_memory_fixes.py::test_directive_standing_rules_stay_eligible now also covers 'You should always tell me before pushing to main.', 'You should always say which files...', 'We should never tell customers...' and 'You should always red-team new prompts...'. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Pair every new rejection pattern with positive directive fixtures.

## 2026-09-23: A Team Norm Was Rejected As A Complaint

**Symptom and cause:** julia_memory_candidates.py:87 'read' in the complaint branch rejects the team norm 'You always read CLAUDE.md...'

**Repair:** Removed 'read' from the always/never complaint list. Added a separate '^you\s+never\s+read\b' branch so the existing complaint case 'You never read the file before editing it.' is still rejected.

**Verification and limits:** test_directive_standing_rules_stay_eligible now covers 'You always read CLAUDE.md before editing files in this repo.'; the existing non-directive test still passes. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Keep complaint patterns narrow and test ordinary standing rules.

## 2026-09-23: Compaction Used A Lossy Summary Silently

**Symptom and cause:** context_budget.py:663 (medium): when the summarizer failed, compaction silently used a lossy fallback summary

**Repair:** summarize_message_batch now marks its local fallback by returning FallbackSummary, a str subclass, so its signature and the existing monkeypatched fakes still work. maybe_compact_context stores str(summary) and, when the fallback was used, calls show_info with a notice that a lossy fallback summary was used (suppressed in JSON mode, as show_info already does). rebuild_context_summary returns that notice as its message. Compaction still goes ahead, so the context window does not overflow.

**Verification and limits:** tests/test_memory_fixes.py: test_summarizer_failure_returns_marked_fallback_summary, test_fallback_compaction_warns_user, test_fallback_manual_rebuild_reports_lossy_summary. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Mark fallback results in the type and tell the user when information was dropped.

## 2026-09-23: Footer Showed Stale Safety State After A Toggle

**Symptom and cause:** main.py:505 - the footer and rprompt showed stale safe/auto/theme/cwd state after a toggle made within the 2 s refresh throttle

**Repair:** build_status_toolbar and format_status_toolbar_plain now read cfg.safe_mode and cfg.auto_approve_active directly. build_status_rprompt reads cwd, theme and memory count directly from cfg. oliver_slash_dispatch.handle_command now calls refresh_after_model_change (a forced refresh_runtime_status plus invalidate_prompt_toolbar) after /safe, /auto, /theme, /cd, /mode, /clear and /policy, through the new _STATUS_REFRESH_COMMANDS set.

**Verification and limits:** tests/test_repl_fixes.py: test_footer_shows_safety_toggle_inside_refresh_throttle, test_rprompt_reads_theme_and_cwd_live, test_state_changing_slash_commands_force_status_refresh[5 commands], test_read_only_slash_command_does_not_force_refresh (guard). tests/test_sticky_status.py::test_format_status_toolbar_plain_matches_key_chips now sets the flags on cfg instead of RUNTIME_STATUS, which is the new source of truth.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Render safety state from live config, not a throttled snapshot.

## 2026-09-23: Ctrl+C At The Prompt Exited With Typed Text

**Symptom and cause:** main.py:5221 - Ctrl+C at the input prompt exited the CLI even with a half-typed line

**Repair:** Added prompt_interrupt_action, PromptInterruptState, read_repl_input and _prompt_buffer_text to main.py; main()'s REPL loop now reads input through read_repl_input. Ctrl+C with text in the buffer clears the line. Ctrl+C on an empty line prints the muted hint 'Press Ctrl+C again or Ctrl+D to exit.', and a second Ctrl+C within 2 s (PROMPT_EXIT_CONFIRM_WINDOW_S) exits. Ctrl+D (EOFError) still exits immediately.

**Verification and limits:** tests/test_repl_fixes.py: test_prompt_interrupt_action[5 cases], test_ctrl_c_with_typed_text_clears_line_without_exiting, test_ctrl_c_on_empty_line_needs_second_press_to_exit, test_slow_second_ctrl_c_only_warns_again, test_ctrl_d_exits_immediately. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Make exit require an empty line and a confirming second interrupt.

## 2026-09-23: Ctrl+C Mid-Stream Dropped The Partial Answer

**Symptom and cause:** main.py:3896 - Ctrl+C mid-stream dropped the partial assistant text, leaving back-to-back user turns

**Repair:** agent_loop's stream try now has 'except KeyboardInterrupt'. When content_text is non-empty it appends {'role':'assistant','content': content_text + GENERATION_INTERRUPTED_MARKER}. It then sets exc.partial_output_kept, closes the stream if it has close(), and re-raises. The REPL prints generation_interrupted_message(exc) in the theme's 'warning' style, stating whether the partial response was kept, instead of the hard-coded '[yellow]Generation interrupted.'.

**Verification and limits:** tests/test_repl_fixes.py: test_ctrl_c_mid_stream_keeps_partial_answer_in_history, test_ctrl_c_before_any_text_reports_nothing_kept. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Persist what the user already saw before propagating an interrupt.

## 2026-09-23: Interrupt Message Misreported Earlier Output

**Symptom and cause:** algo_cli/main.py:3166 generation_interrupted_message said 'No response text was received.' for any KeyboardInterrupt without partial_output_kept, including an interrupt after an earlier round had already streamed text and saved it to history

**Repair:** The message now has three cases. partial_output_kept gives 'Partial response kept in the conversation.' A new no_response_text attribute gives 'No response text was received.' Anything else gives the neutral 'Generation interrupted.' agent_loop keeps a turn-level turn_text_received flag, set when a round's content is saved to history. The stream KeyboardInterrupt handler sets exc.no_response_text = not (content_text or turn_text_received), so the 'no text' message only appears when the whole turn produced no text. Other interrupts, such as a tool-dispatch cancellation or a bare KeyboardInterrupt, now print the neutral message, as they did before this diff.

**Verification and limits:** Added tests/test_repl_fixes.py::test_ctrl_c_after_earlier_round_text_does_not_claim_nothing_received, which reproduces the probe: round 1 returns text plus a list_directory call, round 2 raises KeyboardInterrupt before its first chunk. The test checks that the model was called twice, that the round-1 text is in history, and that the message is exactly 'Generation interrupted.' With the old code it would get 'No response text...'. Also changed the bare-KeyboardInterrupt assertion in test_ctrl_c_before_any_text_reports_nothing_kept to expect the neutral message. The first-round no-text case still asserts 'No response text'.. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Base user-facing messages on recorded state, not the absence of a flag.

## 2026-09-23: Model-Wait Spinner Misjudged Cloud Routes As Local

**Symptom and cause:** algo_cli/main.py:3880 model-wait spinner decides local from cfg.cloud alone, so a ':cloud' model via a signed-in local daemon shows 'loading <model>', and stale cfg.cloud=True without OLLAMA_API_KEY is shown as not local

**Repair:** Added the helper _model_wait_is_local(cfg) in main.py. It returns not (uses_ollama_cloud(cfg) or is_cloud_model_name(cfg.model) or routes_to_xai(cfg) or routes_to_chatgpt(cfg)). The spinner hook now calls model_wait_status(cfg.model, local=_model_wait_is_local(cfg)). The main.py edit is limited to the helper and the one-line hook change.

**Verification and limits:** tests/test_display_fixes.py::test_model_wait_local_flag_follows_actual_ollama_route checks four cases: gpt-oss:120b-cloud with cloud=False gives not local; qwen3 with stale cloud=True and no key gives local; qwen3 with cloud=False gives local; qwen3 with cloud=True and a key gives not local. Local test evidence only; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Derive local-versus-cloud from the same routing helpers the request uses.

## 2026-09-23: Cancelled Team Specialists Kept Running And Rewrote Status

**Symptom and cause:** review of the first cancellation fix found that Ctrl+C
on `/agent team` detached running specialists with `shutdown(wait=False)`, so
they kept using provider time, and a late worker could write `running` over a
child thread already recorded as `cancelled`. The regression test also relied
on a 1.5 s wall-clock bound and failed on hosted Windows at 2.05 s.

**Repair:** Ctrl+C sets the cancellation event first, then waits up to
`TEAM_CANCEL_GRACE_SECONDS` for running specialists and reports any still
running as detached. Specialist thread-record writes go through a team lock
that refuses writes after cancellation, so status can only move forward.

**Verification and limits:** event-driven tests cover cooperative
acknowledgement, a detached non-cooperative worker, a late write that must not
overwrite `cancelled`, and a second Ctrl+C during the lock wait (POSIX only);
a no-op lock reproduces the race. Hosted Windows results are recorded with the
release.

**Prevention:** cancellation tests should synchronize on events, not elapsed
time, and status persistence should be monotonic once cancelled.

## 2026-09-23: Memory Catalog Changed Before The Unreadable-File Check

**Symptom and cause:** with an unreadable `memory.json`, `/memory demote`,
`/memory archive` and pinned `/memory supersede` persisted their catalog change
and then reported that nothing changed, leaving a demoted, archived or
half-superseded record.

**Repair:** those commands run the guarded legacy read before any catalog
mutation, so an unreadable file stops the command with no change.

**Verification and limits:** a parametrized test corrupts `memory.json` and
checks the catalog is unchanged for demote, archive and supersede. Local only.

**Prevention:** run every precondition that can fail before the first durable
write in a multi-store command.

## 2026-09-23: Missing-Model Hint Suggested A Local Pull On Provider Routes

**Symptom and cause:** the new error hint suggested `ollama pull` for any 404
"not found", including direct Ollama Cloud, xAI and ChatGPT routes, where a
local pull cannot help.

**Repair:** the pull hint is limited to routes served by local Ollama
(including `:cloud` models through a signed-in local daemon); other routes are
told to check the model name or pick one with `/models`.

**Verification and limits:** tests cover local, direct-cloud and provider 404s.
Local only.

**Prevention:** derive user guidance from the same routing helpers the request
used.

## 2026-09-23: Hex English Words Were Taken As Thread References

**Symptom and cause:** `/agent resume add a regression test` looked up thread `add`; any all-hex word (`a`, `beef`, `decade`) could select a real thread by prefix and restore its workspace, because the earlier fix accepted 1-64 hex characters.

**Repair:** A reference followed by task words must contain a digit or exactly match an existing thread id; lone references still resolve for feedback.

**Verification and limits:** Tests cover all 16 reported words for resume and fork, digit prefixes, and exact all-letter ids. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Command detection over free text needs a signal English cannot produce, such as a digit.

## 2026-09-23: Schemeless Host Still Disabled Embedding Identity

**Symptom and cause:** `probe_ollama_identity` and the gateway upstream check required an `http` scheme, so `127.0.0.1:11434` produced no identity and bound retrieval silently returned nothing.

**Repair:** Both paths normalize the host with `normalize_ollama_host` before the loopback check.

**Verification and limits:** Tests cover schemeless loopback identity and gateway upstream, and remote hosts still refused. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Fix a normalization gap at every consumer of the value, not only the one that was reproduced.

## 2026-09-23: File Bodies Were Classified As Tool Failures

**Symptom and cause:** `classify_tool_status` sniffed content, so a file whose first line looked like `Error reading /...:` was recorded as failed; three callers also omitted the tool name.

**Repair:** read_file failures are recognized by the tool's own single-line error shape, and `/agent`, one-shot and program runs pass the tool name.

**Verification and limits:** Tests cover real errors, look-alike bodies, and each caller. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Classify by what the tool emitted, not by what the content resembles.

## 2026-09-23: Partial Searches Were Recorded As Success

**Symptom and cause:** Matches plus a trailing partial-search note classified as `worked`, so retry and the attempt ledger treated an incomplete search as complete.

**Repair:** Results carrying the partial-search note are classified as not successful while keeping the matches.

**Verification and limits:** A test classifies the partial note. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** A status that other code branches on must reflect the note the model reads.

## 2026-09-23: Malformed Globs Crashed The Search Fallback

**Symptom and cause:** The new glob-to-regex translation compiled unvalidated character classes, so `*[^]` raised `re.error`.

**Repair:** Invalid or unterminated classes match literally.

**Verification and limits:** A fuzz set of odd globs compiles without exceptions. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Translators from user syntax to regex need a malformed-input test set.

## 2026-09-23: Team Cancellation Did Not Stop Running Tools

**Symptom and cause:** After team Ctrl+C, a specialist already inside `run_shell` or `write_file` could still change the workspace.

**Repair:** `write_file` refuses after cancellation and `run_shell` terminates its subprocess when the team cancellation event is set.

**Verification and limits:** Tests cover both tools with and without cancellation. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Cancellation must reach the operations that mutate state, not only the model loop.

## 2026-09-23: Parallel Tool Batch Ignored Ctrl+C

**Symptom and cause:** After an interrupt the chat loop waited on every running tool future with no timeout.

**Repair:** Not-started futures are cancelled, running ones get a bounded grace, and unfinished calls are recorded as interrupted so tool calls stay paired.

**Verification and limits:** A hung future no longer blocks Ctrl+C and history stays well formed. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Every interrupt path needs a deadline.

## 2026-09-23: Rich Markup Holes Outside Tool Lines

**Symptom and cause:** `/diff` read the block role as a style, and the agent-block panel, `/help`, the memory table and runtime overview parsed untrusted text as markup (`[/tmp/x]` crashed, `d[key]` vanished).

**Repair:** Untrusted values are appended as plain Text across display.py and main.py; `Text.from_markup` is no longer used on them.

**Verification and limits:** Regression tests cover every fixed site; they fail on the previous source. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** After fixing one markup hole, audit the whole module for the same pattern.

## 2026-09-23: Existing Lesson Indexes Kept Old Chunks

**Symptom and cause:** The stale check ignored the index version, and v0.20.0 already wrote version 2, so upgraded installs kept pre-fix chunks.

**Repair:** The version was bumped and a mismatched or missing version rebuilds the index.

**Verification and limits:** Tests include an index written by the released version. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** When changing derived data, bump its version and test the upgrade from the published release.

## 2026-09-23: Lesson Comment Stripping Cut Paragraph Text

**Symptom and cause:** A regex deleted `<!-- ... -->` spans inside paragraphs, cutting text with nested markers.

**Repair:** Only the template's own full comment blocks are removed; paragraph text is kept whole.

**Verification and limits:** The reported paragraph is indexed intact. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Strip only the structure you own.

## 2026-09-23: Memory Filter Exceptions And First-Person Complaints

**Symptom and cause:** `You always needed a backup...` was rejected despite the documented exception, and `I always forget to run the tests` was stored.

**Repair:** The -ed exception is limited to the intended verbs, and first-person complaints are rejected like second-person ones.

**Verification and limits:** A table of accepted rules and rejected complaints covers both directions. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Keep filter changes paired with positive and negative fixtures.

## 2026-09-23: New Files Were Invisible To Code-RAG For 15 Seconds

**Symptom and cause:** The in-memory scan cache checked only files already indexed, so a new file stayed invisible until the cache expired.

**Repair:** The freshness check also compares recorded directory signatures, so additions and removals trigger a rescan.

**Verification and limits:** Tests cover new files, nested and new directories, ignored directories and cache reuse. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** A freshness check must cover additions, not only changes to known entries.

## 2026-09-23: Thinking Panels Hid The Ending And Indentation

**Symptom and cause:** The settled panel showed only the first 1,200 characters, and the live tail stripped leading spaces.

**Repair:** The settled panel shows head, an omission marker and tail; the live window starts at a line boundary and keeps indentation.

**Verification and limits:** Tests check the ending marker and an indented final line. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Truncation should keep the part the reader needs, and never alter the kept text.

## 2026-09-23: Keychain Item Names Were Not Release-Blocked

**Symptom and cause:** The public-release scan blocked personal names but not local keychain service names used for companion credentials.

**Repair:** Those names are private markers in the scan of source and built artifacts.

**Verification and limits:** A regression test proves both names are flagged and generic labels are not. Local test evidence; hosted CI and publication are recorded in `docs/henry-release-0.20.1.md`.

**Prevention:** Treat credential locator names as private data, not only the secrets.

## 2026-09-23: Unpainted Footer Vanished On Light Terminals And Colour Profile Edge Cases

**Symptom:** After the colour-profile slice, the footer and rprompt text (near-white in all seven dark themes) measured 1.07-1.45:1 on a white terminal background, which is macOS Terminal's default profile (`TERM=xterm-256color`, detected as 256 colours). At 16 colours the completion menu painted `bg:default`, so unselected entries sat unpainted over the scrollback, and fenced code under Rich's `ansi_dark` theme had no panel. On `TERM=dumb` or `TERM=unknown` terminals the UI rendered at truecolor, and `FORCE_COLOR=1` in a truecolor terminal dropped the UI to 16 colours.

**Cause:** Removing the bar background assumed the terminal background equals `Palette.bg`, and the contrast gate measured against that assumption. The ANSI palettes set `surface_alt="default"`. `display._profile_active` used Rich's `color_system is not None`, which is also `None` for dumb and unknown terminals, not only for pipes. `detect_color_profile` returned the `FORCE_COLOR` level before checking `COLORTERM` and `TERM`.

**Repair:** `tokens.prompt_toolkit_styles` paints the footer and rprompt on `surface` whenever it is a hex colour; the 16-colour palettes keep `surface="default"` (their `default` text reads on any background) and set `surface_alt="blue"`, with the menu text on `ansiwhite`/`ansigray`. `ui.markdown.PanelledAnsiSyntaxTheme` paints Rich's ANSI token map on `bright_black` (`ansi_light`: `white`). `display._terminal_takes_profile` uses `Console.is_terminal`. `FORCE_COLOR` is now a minimum over the terminal's own detection, as in supports-color.

**Verification:** New regressions in `tests/ui/test_ui_color_profile.py` (every visible footer and rprompt character painted at 256 and truecolor for every theme; ANSI and mono footers unpainted; 16-colour menu entries all painted with blue 44 and the selection on 104; 16-colour code lines inside a 100 panel while prose stays unpainted; `FORCE_COLOR` matrix rows; `_terminal_takes_profile` under `TERM=dumb`; a pseudo-terminal subprocess that reports `none`/`DEPTH_1_BIT` for `TERM=dumb` and `ansi16`/`DEPTH_4_BIT` for `TERM=unknown`). The contrast gate measures chips against their painted bar with both colours quantised. Local offline pytest only; no live terminal matrix was run.

**Prevention:** Never measure unpainted text against an assumed terminal background: either paint the surface or use the terminal's `default` colour. Tell pipes from terminals with `is_terminal`, not with the detected colour system.

## 2026-09-23: Footer Drew A Black Slab And Chrome Rendered At A Different Colour Depth From The Body

**Issue:** The bottom toolbar and rprompt painted `surface_alt`/`surface` backgrounds that read as a black block on any terminal whose background differed from the palette's. Rich picked its own colour depth (truecolor) while prompt_toolkit and `sticky_status` used `ColorDepth.from_env()` (8-bit by default), so body and footer colours disagreed. On 16-colour terminals Rich's quantisation of the hex palettes merged meanings: tokyo-night success and warning, catppuccin-mocha success and error, and nord success, error and info all landed on the same ANSI slot.

**Cause:** Two independent depth detections, painted bar backgrounds in `tokens.prompt_toolkit_styles`, and no palette designed for 16 colours or for `NO_COLOR`.

**Repair:** `algo_cli/ui/detect.py` detects one `ColorProfile` from the environment. `display.py` builds the console at that profile when stdout is a terminal and exposes `prompt_color_depth()` for `PromptSession` and `sticky_status`. `tokens.palette_for()` maps 16-colour terminals to `ansi-dark` (named slots with distinct hues for success, warning, error and info; `ansi-light` is defined for the appearance slice) and colour-off terminals to the attribute-only `MONO` palette, with the safety badges reversed. The footer and rprompt no longer set a background. `/theme` explains when the terminal's profile overrides the hex palettes.

**Verification:** `tests/ui/test_ui_color_profile.py` covers the detection matrix, Rich and prompt_toolkit emitting the same SGR encoding per profile, the sticky footer's depth, no background codes in the footer or rprompt for every theme and profile, distinct 16-colour meanings per theme after rendering, and no colour codes in the mono profile. The existing contrast gate now measures footer chips against the terminal background with only the foreground quantised. Local offline pytest only; no live terminal matrix was run.

**Prevention:** Configure every renderer from `display.active_color_profile()` instead of letting a library guess. When a test renders the same Rich `Style` at several colour systems in one process, give each run its own colour: Rich caches the SGR string on the interned `Style` regardless of colour system, so a 16-colour render otherwise leaks into a later truecolor one.

**Remaining limits:** The unpainted footer was reverted for hex palettes in the entry above. `PROMPT_TOOLKIT_COLOR_DEPTH` no longer overrides the session depth. Light palettes and appearance detection are the next slice.

## 2026-09-23: Theme Styles Were Silently Dropped And Assistant Output Ignored The Theme

**Symptom and cause:** The logo, banner title, error label, thinking text and section headings rendered unstyled in every theme. They used compound styles such as `"bold primary"`; Rich 15 cannot parse a theme name inside a compound style and drops the whole style without an error. Assistant Markdown used Rich defaults and a painted Monokai code block, several footer chips measured below 3:1 on their own bar (dracula muted 2.70, nord error 2.15 after 8-bit quantisation), the completion menu kept prompt_toolkit's light-grey defaults, redeye used red for brand, error and info alike, and `/reload` left the previous theme's footer style in place.

**Repair:** `algo_cli/ui/tokens.py` holds one palette table per theme and generates `display.THEME_COLORS`, `display.THEME_MAP` and the prompt_toolkit style. Semantic tokens carry their attributes (`brand.logo`, `heading`, `notice.error`, `thinking`), and 30 compound call sites now use them. Rich built-ins (`markdown.*`, `repr.*`, `rule.line`, `status.spinner`, `table.header`, `prompt.*`) are overridden per theme, and `display.themed_markdown()` uses a per-theme Pygments style. Footer chips are prompt_toolkit classes from the same table, and `main.apply_theme()` serves both `/theme` and `/reload`. Palettes were tuned to the contrast floors, identical tokens were split, and redeye error and info moved off red.

**Verification and limits:** `tests/ui/` checks that every theme defines every token and that each token parses. It also checks contrast floors against each palette's assumed dark background (text 7:1, muted and semantic colours 4.5:1, borders 3:1, footer chips 4.5:1 on their bars in truecolor and 8-bit), pairwise ΔE2000 of at least 20 between success, warning, error and brand, a lint that bans compound theme styles with a shrinking allow-list for raw colours, recorded renders of the banner, logo, error, thinking and Markdown in all seven themes, and the `/theme` and `/reload` style rebuild. These are local test results. Light terminals, 16-colour quantisation, and the colour-depth mismatch between Rich and prompt_toolkit remain open for later slices.

**Prevention:** Never compose attributes with a theme name at a call site. Add a token that carries the attributes, and let the lint and the completeness test keep the table whole.

## 2026-09-23: Themed Code Blocks Lost Their Panel And Some Tokens Fell Below Readable Contrast

**Symptom:** After the theme-token slice, fenced code blocks drew with `Syntax(background_color="default")`. On a light terminal the dark-theme token colours measured 1.07 to 2.4:1. The gruvbox `diff` lines were `#282828` on `#282828`, because the default background also replaced the red and green token backgrounds. dracula `Generic.Deleted` `#8b080b` measured 1.45:1, and nord comments `#616e87` measured 2.43:1.

**Confirmed cause:** Unpainting the panel put code-theme colours, which assume a dark background, on whatever background the terminal uses, and removed per-token backgrounds. The dracula and nord Pygments styles also ship tokens below any text floor, and the contrast gate never checked code-theme tokens.

**Repair:** `ThemedCodeBlock` paints the Pygments style's own background again. `ui.markdown.ReadableSyntaxTheme` moves any token colour below 4.5:1 against its effective background (token background, else the panel) toward white or black with `contrast.lift_to_floor`. Inline code and all other Markdown stay unpainted.

**Verification:** `tests/ui/test_ui_code_blocks.py` checks every token of every theme's code style against the floor. It also renders `diff` and Python fences in all seven themes and checks that each glyph has a painted background and meets 4.5:1 on it. The new tests fail against the previous renderer and also against a painted but unlifted renderer. These are local test results only.

**Prevention:** Any colour drawn by a third-party style belongs in the contrast gate, together with the background it is actually drawn on.

## 2026-09-23: Colour Profile Ignored The Env File And Late Windows VT Enablement

**Symptom (PR #74 review):** `NO_COLOR`, `COLORTERM` or `FORCE_COLOR` set in `~/.algo_cli/env` or `ALGO_CLI_ENV_FILE` had no effect on colour depth. A Windows console whose VT mode `_force_utf8_console` enabled at startup stayed at ANSI16.

**Confirmed cause:** `display.COLOR_PROFILE` and the shared Rich console were fixed when the module was imported. That happened before `main()` ran `_force_utf8_console()` and `load_runtime_env(override=True)`.

**Repair:** `display.refresh_color_profile()` runs detection again and re-probes the terminal and `legacy_windows`. It then updates the shared console in place, because other modules hold it by reference: it sets `legacy_windows`, the colour system and `no_color`, and pushes the base and active themes again at the new profile. `main()` calls it immediately after `load_runtime_env`. The prompt session and sticky footer already read `active_color_profile()` when they render, and they are created after that call. Import-time detection still gives tests and one-shot runs a safe provisional value.

**Verification:** `tests/ui/test_ui_startup_profile.py` runs `main()` with an env file that sets `NO_COLOR=1` and checks the NONE profile, a 1-bit prompt depth and `console.no_color`. Another test simulates enabling VT after import and expects ANSI16 to become truecolor, and a third checks that the active theme is re-applied. All three fail against the previous source. These are local test results; no Windows console was exercised.

**Prevention:** Anything derived from the environment must be read after startup environment setup, or refreshed at that point.

## 2026-09-23: Mono Prompt Style Used "dim", Which Older prompt_toolkit 3.0.x Rejects

**Symptom (PR #74 review):** The colour-off prompt_toolkit style set `dim`. The dependency floor is `prompt-toolkit>=3.0`, and on older versions in that range `Style.from_dict` raises. The PromptSession style and the sticky footer are then silently lost.

**Confirmed cause:** Wheels checked locally show that `Style.from_dict({"x": "dim"})` fails with "Wrong color format 'dim'" in 3.0.0, 3.0.36, 3.0.43, 3.0.47, 3.0.48, 3.0.50 and 3.0.51, and first succeeds in 3.0.52 (the locked version). `bold`, `italic`, `underline`, `reverse`, `noreverse` and `hidden` parse in all of those versions.

**Repair:** In the mono prompt_toolkit map, `dim` is replaced with `italic` (muted text and completion meta) and plain text (separators). The dependency floor is unchanged.

**Verification:** New tests in `tests/ui/test_ui_startup_profile.py` restrict every prompt_toolkit style string, for every palette and profile, to the attributes that 3.0.0 through 3.0.51 accept. They fail against the previous tokens. The Rich-side mono tokens still use `dim`, which Rich supports.

**Prevention:** A prompt_toolkit style attribute must parse at the declared dependency floor, not only at the locked version.

## Repair Log Checklist

- Date and component.
- Observed symptom and confirmed cause; identify anything still unknown.
- Exact repair and its local, deployed, or operational scope.
- Verification commands, results, and evidence links.
- Prevention or regression coverage, plus remaining limits.

Never include credentials, personal state, raw sensitive logs, or unsupported
success claims. A failed check remains a failed check even after later recovery.
