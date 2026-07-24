# Algo CLI Arthur Hardening Master Plan

Status: **ACTIVE DEVELOPMENT FREEZE**
Freeze baseline: `20bb879` (`v0.18.0`)
Authoritative state: `hardening/henry-freeze.toml`
Requirement ledger: `hardening/ada-evidence-ledger.json`

## 1. Non-negotiable rule

Until every ledger requirement is verified, the repository permits only changes that directly close a recorded hardening requirement or produce authoritative evidence for it.

The following are prohibited during the freeze:

- New user-facing features.
- Marketing or benchmark claims.
- Unrelated refactors or cleanup.
- Registration of browser, desktop, Accessibility, screen-capture, Apple Events, clipboard, upload, or download tools.
- Release creation, tagging, PyPI publication, website publication, or lifting existing safety restrictions.
- Declaring work complete from intent, code review, a narrow test, or the absence of an observed failure.

If hardening requires a new tool, kernel, parser, simulator, fuzzer, ledger, or native component, it may be built only for a listed requirement, must be disabled from normal runtime use until its milestone passes, and must receive adversarial tests before integration.

## 2. Naming contract

Naming is determined by a file's primary responsibility, with this precedence:

1. Memory/state/artifact/receipt/telemetry files contain a female name.
2. Browser/Chrome/Playwright/extension files contain a chemical element name.
3. Computer-use/macOS/Accessibility/capture/XPC/TCC files contain a city name.
4. Process/runtime/dispatch/effect/approval/capability/QoS files contain a male name.

Files outside those categories retain normal project naming. The gate tokenizes the complete basename on punctuation and case boundaries. Existing category files may not be modified under a nonconforming name; they must either remain untouched or be deliberately migrated with all import, packaging, and compatibility evidence recorded. Deletions are audited but do not create a new nonconforming artifact.

Every changed or created path must also appear in the evidence ledger's `authorized_paths` before the change is made.

## 3. Foundation invariants

The hardening phase must prove all of these invariants:

1. Unknown authority fails closed.
2. Model output and page/UI text are untrusted data, never permission.
3. Static metadata describes maximum possible effects; runtime policy resolves the exact action.
4. The model cannot mint grants, confirmations, permits, receipts, or successful outcomes.
5. Auto mode cannot bypass action-time confirmation or required handoff.
6. A timeout does not prove cancellation; uncertain mutations become `unknown_outcome`.
7. Unknown mutations are never retried automatically.
8. A mutation is successful only after a fresh target-bound postcondition.
9. One target has one active writer, protected by a monotonically increasing fencing token.
10. Safety or permission denial is terminal and cannot trigger a weaker fallback.
11. Observations sent to a remote model are data egress.
12. Raw page, AX, screenshot, credential, upload, download, or browser-state data never enters telemetry or durable memory by default.
13. Browser state isolation is not treated as host isolation.
14. The networked Python/model runtime never owns macOS TCC control authority.
15. Local receipts are described as signed and tamper-evident, never immutable.

## 4. Milestone dependency graph

```text
M0 Freeze/baseline
 └─ M1 Policy authority
     └─ M2 Outcomes/effects/concurrency
         └─ M3 Privacy/storage/plugins/program safety
             └─ M4 Custom control kernel + hostile simulators
                 ├─ M5 Isolated browser foundation
                 └─ M6 Signed macOS foundation
                     └─ M7 Packaging/readiness/supply chain
                         └─ M8 Adversarial verification/efficiency
                             └─ M9 Requirement audit/freeze lift
```

No milestone may begin its mutating integration work until all predecessors are verified. Research, fixtures, and test design may proceed early, but cannot be used to bypass a predecessor gate.

## 5. Milestones

### M0 — Freeze and baseline

Deliverables:

- Active freeze manifest and release-event block.
- Machine-enforced authorized-path and naming checks.
- Requirement/evidence ledger.
- Baseline full tests, Ruff, mypy, coverage, package build, public-source scan, history scan, dependency audits, and existing benchmark snapshot.
- A hardening-only branch rooted at the frozen commit.

Exit criteria:

- Freeze tests prove releases fail while the freeze is active.
- Undeclared files and each naming-category violation are rejected.
- The freeze gate parses the actual tool, ActionSpec, kernel, and slash registries
  and rejects interactive browser/computer names, disabled-boundary imports from
  runtime entry modules, or a production native-control activation resource.
  These checks remain mandatory until the audited lift; documentation alone is
  never evidence that an unfinished capability is unreachable.
- Baseline failures, if any, are recorded rather than silently reclassified.
- Every later requirement has an identifier and evidence contract.

### M1 — Fail-closed policy authority

Deliverables:

- `ActionSpecV2` with effect class, maximum risk, data classes, exact capability vocabulary, confirmation mode, idempotency class, outcome model, verification requirement, and equivalent-fallback group.
- `ResolvedAction`, `ConsentGrant`, and one-use `ActionPermit` schemas.
- Explicit capabilities for browser read/navigation/input, desktop observation/input, capture, Accessibility, Apple Events, clipboard, upload, download, and data egress.
- Four confirmation modes: none, scoped session preapproval, action-time, and handoff required.
- An injected approval strategy replacing global monkeypatches.

Required adversarial tests:

- Generated `browser_click` metadata fails registry readiness.
- Unknown tools cannot become read-only, retry-safe, or approval-free.
- Auto and one-shot execution cannot bypass action-time/handoff policy.
- A permit for one target, revision, operation, data class, or expiry cannot authorize another.
- Policy-derived approval, capability, retry, QoS, and verification fields cannot disagree.

### M2 — Typed outcomes, external effects, and concurrency

Deliverables:

- Typed outcomes: succeeded, failed, denied, skipped, timed out, cancelled, and unknown outcome.
- One canonical dispatcher for chat, agent pipelines, one-shot, programs, and future control clients.
- Crash-safe effect lifecycle: discovered, resolved, preflighted, authorized, leased, dispatch-intent persisted, dispatched, observed, then verified/failed/unknown.
- Retry classes: pure, idempotent, at-most-once, non-idempotent, and compensatable.
- Target leases, monotonic fencing tokens, deadlines, cancellation, postconditions, idempotency IDs, reconciliation, and explicit compensation metadata. No released action may claim `compensatable` until a reviewed inverse action and snapshot-backed proof exist; compensation must be a new approved dispatch, never an implicit rollback.

Required adversarial tests:

- Structured error JSON is never classified as success.
- Crash or timeout before dispatch differs from crash or timeout after dispatch.
- Unknown mutations never retry automatically.
- Late replies and expired lease holders cannot commit success.
- Two agents cannot mutate the same tab/window concurrently.
- Clock jumps, duplicate messages, replayed IDs, and partial batches fail safely.

Verified implementation state (2026-07-19): all model-originated chat, agent,
one-shot, and typed-program actions enter `james_dispatch`; status is carried
out-of-band through the pipeline and one-shot bridge; external effects use the
Clara v2 append-only state machine and Henry cross-process target fences. A
synchronous Python adapter remains cooperatively cancellable rather than
forcibly preemptible, and an external action without an independent verifier is
reported as `unknown_outcome`. Browser tab/document fencing remains an M5
specialization of the generic target fence, not a claim about a released browser
feature.

### M3 — Privacy, storage, plugins, program safety, and supersession

Deliverables:

- Recursive schema-aware redaction with separate confirmation, model, audit, and telemetry views.
- Keyed digests for sensitive attempt identity without persisting plaintext.
- Encrypted short-lived artifact storage with TTL, quotas, per-run access, integrity, cleanup, revocation, and crash recovery.
- Telemetry allowlists limited to structural counters and error classes.
- Full-program argument validation before step one, sensitivity/untrusted taint tracking, whole-plan effect preflight, cancellation, and reconciliation.
- Plugin path containment, manifest-name validation, duplicate rejection, no core-tool override, and prohibition on privileged plugin contributions.
- External target epochs for browser/desktop supersession.

Required adversarial tests:

- Nested secrets, URLs, form values, DOM/AX data, screenshots, and selectors do not leak.
- Untrusted observations cannot flow into shell, credentials, uploads, messages, or publication without a policy boundary.
- Malformed later steps cannot fail only after an earlier external mutation.
- Plugin traversal, symlink escape, name collision, and in-process privilege claims reject.
- Artifact quota, TTL, deletion, permissions, incomplete write, integrity failure, and restart recovery pass.

#### M3 storage decision record

The artifact boundary uses AES-256-GCM from `cryptography`, 96-bit random
nonces, and domain-separated HKDF-SHA256 subkeys. Each artifact receives a
different derived encryption key as well as a random nonce, and the run and
artifact identifiers are authenticated as associated data. This follows the
`cryptography` AEAD contract and NIST SP 800-38D; no custom cipher, tag, or
nonce construction is used.

The Grace master key must come from a recognized OS credential-store backend.
Null, fail, chained, and third-party plaintext keyring backends reject. The
keyring project's macOS security note is an explicit limitation: another
script launched by the same Python executable may be able to access a Keychain
item without a new prompt. Alice is therefore encrypted local storage, not the
signed application-identity boundary. The signed native broker in M6 must own
that stronger identity.

Each Alice run has a random 256-bit bearer capability that is never persisted.
Its verifier, run salt, quotas, TTL, revocation state, and opaque artifact
records live in an HMAC-authenticated manifest. Ciphertext publication precedes
manifest commitment so incomplete writes are recognizable as orphans.
Revocation commits the signed revoked state before best-effort unlink. Global
Henry leases serialize quota checks across processes; expired/revoked runs and
crash staging are pruned inside the same write lease. Cleanup authenticates
every active ciphertext and repairs only signed counter drift, never corrupt
active data.

The implementation does not claim secure erasure: APFS snapshots, backups, or
copied ciphertext can outlive unlink. It also does not call hash-linked local
receipts immutable or signed; signed receipt sequence heads remain M7 work.
Sensitivity-aware suppression of ordinary result hashes and previews remains
part of HARD-033, so HARD-031 verification does not imply the whole M3 privacy
surface is complete.

Primary design references:

- `cryptography` authenticated-encryption documentation:
  <https://cryptography.io/en/stable/hazmat/primitives/aead/>
- `cryptography` HKDF documentation:
  <https://cryptography.io/en/stable/hazmat/primitives/key-derivation-functions/>
- NIST SP 800-38D:
  <https://csrc.nist.gov/pubs/sp/800/38/d/final>
- Python keyring security considerations:
  <https://keyring.readthedocs.io/en/stable/#security-considerations>

#### M3 plugin decision record

William treats local `plugin.json` files as untrusted discovery metadata only.
Python's import protocol calls `exec_module()` to execute module code, and PyPA's
plugin guidance likewise describes entry-point `load()` as importing the chosen
module. An in-process Python plugin consequently has the process's ambient
authority; path validation, audit hooks, or contribution filtering cannot make
its import a sandbox. Algo therefore disables `__init__.py` import and all
callable action/tool/slash contribution points during hardening. No local plugin
execution route is enabled; a future finite gateway adapter would require its
own review and ordinary dispatcher integration.

The manifest reader uses a closed version-1 schema, duplicate-key detection,
strict UTF-8 and size limits, canonical lowercase ASCII names equal to their
directory, exact fields, and explicit rejection of executable/privileged
contributions. Discovery rejects symlink/junction escape, special files, hard
links, group/world-writable plugin state, root escape, stale manifest objects,
and duplicate canonical identities. On platforms that expose it, `O_NOFOLLOW`
is combined with before/opened/after file identity checks. This reduces
check/use substitution risk but is not advertised as protection from an
attacker who already controls the Algo process or user account.

Primary design references:

- Python import execution model:
  <https://docs.python.org/3/reference/import.html>
- Python `importlib` loader contract:
  <https://docs.python.org/3/library/importlib.html>
- PyPA plugin discovery and entry-point loading:
  <https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/>
- Python filesystem descriptor and no-follow guidance:
  <https://docs.python.org/3/library/os.html>

#### M3 external-supersession decision record

External observations cannot use tool name and arguments as sufficient snapshot
identity. Chrome associates a frame with a loader and documents that
`Page.frameNavigated` moves the frame to a new loader; `Page.reload` can require
the expected loader specifically to prevent a racing navigation from acting on
an unintended target. Same-document navigation is separately observable and
does not necessarily change the committed loader. The W3C WebDriver standard
likewise defines an element as stale when its node is no longer in the active
document or is disconnected. On macOS, Apple documents both invalid
`AXUIElement` references and messaging failures/timeouts, so a desktop object
reference cannot be treated as durable evidence across application lifecycle
changes.

Evelyn therefore requires a closed, HMAC-authenticated version-1 binding on the
top-level tool result. It contains only target kind, keyed target ID, positive
epoch, bounded opaque revision, nonnegative fencing token, schema version, and
authentication tag. The raw target is never serialized. Assistant metadata and
model-authored arguments cannot supply the binding. For the only currently
admitted external family, `web_fetch`, the binding must be an
`external_resource` and its target ID must match the exact requested URL, which
prevents a captured tag from being replayed onto another invocation target.

The O(n) pass enforces monotonic epochs and fencing per target. Same epoch and
fence with a changed revision is invalid. Any regression disables compaction
for that target without affecting independent targets. Missing bindings,
protocol gaps, target/generation changes, and A-to-B-to-A histories create
non-crossing segments. External observations never enter pair-deleting count
pruning. Older eligible external content receives a keyed HMAC content ID,
never a public SHA-256 fingerprint that could support offline guessing of
sensitive page content. HMAC is used as a shared-secret authentication code,
not described as a public-key signature or an immutable receipt.

The current `web_fetch` adapter does not issue target bindings, so it remains
conservatively ineligible. M5 and M6 must issue bindings from independently
observed browser document and desktop surface lifecycles before they can extend
the external allowlist. If the OS-backed Irene key is unavailable and the
runtime falls back to a process-local key, bindings from another process fail
authentication and preserve full evidence rather than compacting it.

Primary design references:

- Chrome DevTools Protocol Page domain (`loaderId`, navigation, reload race
  protection, and same-document navigation):
  <https://chromedevtools.github.io/devtools-protocol/tot/Page/>
- W3C WebDriver element freshness and active-document rules:
  <https://www.w3.org/TR/webdriver2/#dfn-stale>
- Apple `AXUIElement` lifecycle and error contract:
  <https://developer.apple.com/documentation/applicationservices/axuielement_h>
- Apple invalid accessibility-object result:
  <https://developer.apple.com/documentation/applicationservices/axerror/invaliduielement>
- NIST FIPS 198-1 HMAC specification:
  <https://csrc.nist.gov/pubs/fips/198-1/final>
- Python HMAC and constant-time comparison contract:
  <https://docs.python.org/3/library/hmac.html>

Verified component state (2026-07-19): HARD-030 recursive privacy views,
HARD-031 encrypted artifact storage, HARD-032 telemetry allowlists, HARD-033
whole-program preflight/taint, HARD-034 manifest-only plugin hardening, and
HARD-035 epoch-bound external supersession pass the 2,176-test repository
regression, full Ruff, diff hygiene, compile checks, an expanded 24-module mypy
contract, and the freeze gate. The typed-program cell records 96.644 percent
intermediate-token reduction with correct output, encrypted artifact backing,
and valid receipt chains. The authenticated external-supersession cell records
78.98 percent reduction and 29,860 estimated tokens saved while preserving the
latest result and using only HMAC content IDs. Both are synthetic local
measurements, not live-provider, cross-harness, or marketing claims. M3 is
verified; concrete browser and desktop epoch issuers remain gated in M5 and M6.

### M4 — Custom control kernel and hostile simulators

Build the control kernel from scratch as a provider-neutral closed DSL. It must accept only finite typed actions and must never evaluate model-authored JavaScript, AppleScript, JXA, shell, Python, CSS, CDP, or generic programs.

Deliverables:

- Strict versioned schemas with `additionalProperties: false`, canonical identifiers, numeric/size/depth bounds, deadlines, sequence numbers, request IDs, grants, permits, snapshots, effects, outcomes, and signed receipts.
- Broker-independent policy revalidation.
- Deterministic route ordering: purpose-built connector, approved Shortcut, reviewed Apple Event adapter, DOM/AX semantics, screenshot, bounded coordinates, then handoff.
- Hostile in-memory browser and desktop simulators.
- Length-framed protocol fuzzer and crash injector.

Exit criteria:

- 100,000 malformed envelopes produce zero crashes/OOMs.
- 100% of stale, replayed, downgraded, over-limit, wrong-target, expired, and revoked permits reject.
- Fault injection covers every state transition and proves no duplicate uncertain mutation.

M4 decision record (2026-07-19): the disabled foundation is divided into the
David finite protocol/policy kernel, Ada private durable journal, David
dispatch-once/reconciliation runtime, and Neon/Austin hostile lifecycle
simulators. There is no normal action-registry entry, arbitrary-code field,
browser process, native framework, or generic program route. Exact grants and
one-use permits are Ed25519 authenticated and bind the request digest, target
kind/ID/epoch/revision/fence, snapshot, effects, data class, routes, byte limits,
deadline, action count, policy, and authority key. Claim is atomic; revocation,
expiry, policy, live routes, and live snapshots are independently checked again
before `started`; recovery never redispatches an uncertain effect.

The focused matrix passes 170 tests. The repository passes 2,346 tests, Ruff,
compileall, diff hygiene, an expanded 25-file mypy boundary, the freeze gate,
and strict dependency audit. Draft 2020-12 reference validation passes all five
exported schemas and canonical instances, with zero trailing-newline mismatches.
The final deterministic 100,000-case run rejected every case, produced zero
unexpected accepts or crashes, and stayed within a 2,844-byte observed decoder
buffer. All eight injected crash checkpoints dispatch and mutate at most once.

The adversarial work found two real bugs before closure: noncanonical Base64URL
signature aliases and JSON Schema `$` line-end behavior that admitted trailing
newlines in 63 string positions. Canonical signature re-encoding and absolute
ECMA-262-compatible schema end guards close both gaps. M4 is verified only for
this disabled Python foundation and simulators. Real Chrome/process/network
isolation remains M5; signed/notarized macOS/TCC behavior remains M6; durable
key heads, package identity, and supply-chain claims remain M7.

### M5 — Isolated browser foundation

This milestone is hardening infrastructure, not a released browser feature. Nothing is added to the normal action registry.

Execution routes:

1. Structured connector/API.
2. Current stable Chrome/Chromium in an ephemeral Algo-owned profile under OS/process/network isolation.
3. Pinned Chrome for Testing only for trusted fixtures and benchmarks.
4. A selected existing-Chrome tab through a minimal Manifest V3 extension.

Mandatory controls:

- No normal-profile cloning, cookie/storage/password/history access, open CDP TCP port, arbitrary JavaScript, raw CDP, generic selectors, or debugger permission.
- MVP extension permissions limited to activeTab, scripting, nativeMessaging, storage, and incognito disabled.
- User gesture required for the selected tab; cross-origin navigation revokes the grant.
- Actions bind paired profile, window, tab, top/frame document, canonical origin, snapshot/revision, opaque element token, operation set, action count, expiry, and fence.
- External egress enforcement blocks loopback, private/link-local ranges, metadata endpoints, local discovery, denied redirects, DNS rebinding, unauthorized WebSockets, and direct bypass.
- Downloads disabled by default; uploads count as transmission at file selection.
- Dialogs, popups, service-worker restart, extension/native version skew, BFCache, prerender, frames, shadow DOM, canvas, PDFs, internal URLs, auth, passkeys, CAPTCHA, and user intervention have explicit fail-safe behavior.

#### M5 split-horizon browser decision record

The browser boundary is deliberately divided between independently enforced
layers. The Xenon egress broker owns canonical URL/origin policy, DNS answer-set
poisoning, re-resolution, direct pinned-address connections, connected-peer
verification, TLS validation, bounded HTTP parsing, Upgrade rejection, redirect
validation, byte/connection/deadline limits, and content-free receipts. The
Boron wrapper owns Chromium process identity, the file-descriptor 3/4 ASCIIZ
DevTools pipe, a finite packaged method vocabulary, top-frame/loader/document
lifecycle, dialog/popup/file-chooser/download failure, explicit WebSocket URL
blocking, unexpected-WebSocket detection, and post-navigation origin checks.
Neither layer exposes raw CDP, arbitrary JavaScript, a debugger port, a generic
proxy, or caller-supplied Chrome flags to the model.

This split is necessary because Chromium's current proxy contract permits
`http`, `https`, `ws`, and `wss` through an HTTP proxy, while HTTPS `CONNECT`
normally hides the application request. A tunnel-only proxy therefore cannot
honestly prove WebSocket or redirect policy. Public eligibility requires the
broker's ephemeral TLS interception boundary, with upstream certificate
validation and no persisted CA key, plus wrapper-side network blocking. A
failed certificate import, TLS peer check, HTTP parse, blocked-protocol event,
unexpected target, lifecycle mismatch, or broker/wrapper disconnect fails the
session closed. This is defense in depth: the browser remains on an internal
network with no direct route even if either application policy has a bug.

The selected existing-Chrome package remains a separate observe/handoff-only
route. Its minimal `activeTab` grant must never be widened to compensate for a
managed-route limitation. Chrome for Testing remains fixture-only. Until a
current-stable digest-pinned public image, the dual-homed broker, the finite
wrapper, and a live end-to-end session all pass, HARD-050 stays in progress.

The 2026-07-19 local cells prove the exact digest-pinned browser and broker
builds plus the isolated topology, but not a complete public-browser session.
The freshness gate now correctly distinguishes release age from update lag: a
fresh, bounded Google VersionHistory observation identifies Linux stable
`150.0.7871.128`, so the matching public image has zero update lag. The prior
release-age calculation was a real logic bug because it would reject even the
latest browser whenever vendors went more than 72 hours without a release.

The amd64 Chrome sandbox fails under QEMU on this arm64 host, so live evidence
now refuses emulation rather than producing a misleading product failure or
passing with `--no-sandbox`. The optional native arm64 Debian Chromium image is
also ineligible: upstream security-patch equivalence has not been proven, and
package age is not a substitute for update lag. A pinned `ubuntu-24.04` CI cell
now builds the public images once and requires five distinct fresh-ephemeral
browser/broker sessions on native amd64 Linux. Its bounded report reconstructs
the exact build and live schemas, rejects image changes or reused topologies,
binds the workflow and relevant source, publishes denominators, Wilson 95%
intervals and p50/p95 duration, and is provenance-attested on push. It remains
explicitly ineligible for public product or benchmark claims. The cell has not
executed on a hosted revision yet, so M5 stays in progress until that real run
and its attestation are reviewed.

### M6 — Signed macOS computer-use foundation

This milestone is also disabled from normal runtime use until the final audit.

Deliverables:

- Direct-distributed Developer ID app and user LaunchAgent with hardened runtime, minimal entitlements, notarization, stapling, and Gatekeeper validation.
- Authenticated XPC peer requirements, per-connection capabilities, no root daemon, no open TCP listener, no model/network credentials, and independent native confirmation.
- Separate picker-scoped and persistent programmatic ScreenCaptureKit paths.
- Ephemeral AX target binding and reviewed fixed Apple Event adapters.
- Bounded CGEvent fallback without Input Monitoring.
- Stable permission identity and upgrade/reinstall/rollback/uninstall behavior.

Required adversarial matrix:

- Unsigned, ad-hoc, wrong-team, same-team wrong-bundle, wrong-user/session, replay, downgrade, oversized, and crashed peers.
- Permission denial/revocation/regrant, screen lock, sleep, fast-user switching, display changes, app relaunch/PID reuse, focus theft, modals, hung targets, secure fields, localization, multiple windows/processes, points/pixels/scaling, keyboard layouts, IME, and user interleaving.
- No blind retry after AX cannot-complete, Apple Event timeout, Shortcut timeout, or broker disconnect.

#### M6 native authority decision record

The native boundary is split into an App-Sandboxed relay and a narrow user
LaunchAgent TCC adapter. The relay has the exact app-group entitlement and no
network entitlement. The unsandboxed adapter is required for general
Accessibility control; it has no network entitlement, framework, socket API,
listener, subprocess, script engine, clipboard, event tap, keyboard injection,
silent permission prompt, model credential, or arbitrary target/event input.
This is an audited networkless adapter, not a claim that App Sandbox prevents
its network access. Mutual signed XPC, same-user graphical-session validation,
per-connection capabilities, the independently verified Ed25519 envelope, and
the durable permit claim form the effect boundary.

The current Swift foundation implements strict canonical wire handling,
Developer ID requirement construction, PID-start binding, exact authority
cross-binding, durable concurrent replay exclusion, bounded ephemeral AX and
coordinate bindings, one-frame locally redacted capture leases, and two fixed
Apple Event adapters. Its ScreenCaptureKit bridge has distinct picker and
persistent filter providers, a two-second one-frame bound, cursor/audio
suppression, bounded downscaling, and RGBA conversion for local redaction. The
public system-picker source is main-thread and user-gesture gated and stores one
single-window or single-display filter for one consumption; explicit shutdown
revokes an unconsumed selection. Alice stores already-redacted frames only as
AES-256-GCM ciphertext in an existing exact-0700 directory, with exclusive
0600 files, bounded TTL/quota, exact receipts, expiry cleanup, and revocation.
One nonblocking exclusive directory lease prevents a second live sink from
misclassifying active files as crash orphans. Startup validates the complete
directory before mutation, removes only exact owner-private bounded Alice
orphans whose in-memory consumer authority cannot have survived, and preserves
and rejects unknown, linked, or otherwise suspicious entries. Authenticated
corruption consumes the capability, scrubs its verifier, deletes the ciphertext,
and syncs the directory before the failure returns.

The foundation also contains a fixed-copy native confirmation alert with a
30-second maximum timeout and a one-second user-presence lease. Public
workspace session and screen-sleep notifications invalidate that lease; wake
or switch-back never restores it. This is deliberately not represented as an
Apple screen-lock oracle. A one-shot Shortcuts review handoff opens one fixed
editor target and cannot run a workflow, accept input or clipboard data,
register callbacks, or invoke a subprocess. AX text is always handoff.
Secure/auth/payment/unknown fields and system/auth/payment modals are handoff.
AX cannot-complete, Apple Event timeout, generic button presses without
postconditions, and generic coordinate clicks are unknown and never retried.
The generic `activate` operation may no longer select the coordinate fallback;
only the explicitly bounded `coordinate_activate` operation may do so.

The target-free discovery gap now has a two-phase local implementation. Samuel
signs a one-use `control.prepare` authorization over the future request ID,
subject, operation, data class, exact native route, reviewed selector, bounded
structural arguments, and a 60-second maximum window. The adapter verifies and
durably claims that preparation before Thomas invokes fixed-copy confirmation
and derives an opaque native binding. The returned target/snapshot/element or
viewport fields must then be incorporated into the separately signed
target-bound execution envelope. A strict Python consumer rejects noncanonical
replies, mismatched signed partial arguments, substituted target/snapshot
generations, and expired route-specific bindings before it can construct that
envelope. The dispatcher cross-checks the future request
ID, subject, operation, data class, route, target/snapshot/fence, canonical
argument digest, expiry, and one-use state before the normal permit claim.
Adapter bind APIs consume a scoped atomic confirmation lease instead of action-
confirmation booleans. Python and Swift share an exact signed preparation test
vector, and the fixed selector matrix excludes AX text, caller-defined Apple
Event targets, and CLI-triggered picker capture.

Both DEBUG and RELEASE compile, 89 Swift tests pass, the staged five-binary audit
passes, and a real ad-hoc user-LaunchAgent XPC probe observes zero adapter
TCP/UDP sockets. That probe uses an explicitly empty DEBUG relay entitlement
because AMFI rejects an App-Sandboxed ad-hoc relay before `main`; it is not
production App Sandbox, signed-identity, TCC, notarization, or Gatekeeper
evidence. The staging work found and fixed a 30-byte base64url key-decoding bug
and a helper-bundle resource lookup bug. Production startup now validates the
sealed outer app and exact 32-byte read-only authority key before accepting it.
The app also stages a networkless, empty-entitlement `neon-native-host` guard.
It validates the sealed bundle and an exact pinned, non-writable extension
origin, then remains deliberately protocol-disabled with zero stdout. A new
`macos-15` hosted CI cell compiles DEBUG/RELEASE Swift, stages and audits the
bundle, and exercises the three negative host cases. No hosted run has executed
yet, so that workflow is enforcement source rather than remote evidence.

M6 remains in progress. This host has no valid Developer ID identity. The normal
LaunchAgent now enters a fail-closed production factory: a missing activation
resource yields the disabled dispatcher, while a present resource must be
canonical, code sealed, sorted, and limited to AX, reviewed Apple Events,
review-only Shortcuts, and bounded coordinates. The factory assembles the real
system backends without requesting permission or taking an action, but no
enabled production profile has been signed or qualified. Picker selection is
now bound locally to one macOS 15.2+ window or display ID plus exact geometry,
stored with the one-use lease, and revalidated before capture. Older picker-
only systems fail closed before UI, cross-mode selection is rejected before
pixel acquisition, and the classifier must pass a content-free availability
preflight before native confirmation. Persistent capture binds that exact
classifier and a content-free preparation/request/subject/data-class context
into the one-use lease. The context is revalidated before capture; pixel-
dependent classification occurs only after the bounded frame exists and before
`frame.redact` or `sink.acceptRedacted`. Empty, invalid, or over-broad plans
fail closed and cannot be persisted. Frame capture validates every redaction
region in a first pass and rejects aggregate redaction work above one frame's
pixel count before mutating any byte, preventing overlapping-region CPU
amplification.

An internal, deliberately unqualified Vision candidate now detects only text
rectangles and face rectangles; it performs no OCR, recognized-string, or
candidate extraction. Detector output is capped, merged with bounded work, and
falls back to full-frame redaction for private, empty, overloaded, or fragmented
results. It is fixture-qualified but has no live accuracy corpus and is not
assembled by the production Thomas factory. Alice now has a non-interactive,
local-device Keychain factory in the adapter's default code-signing access group
and a sealed module-local Isaac pipeline. That pipeline encrypts only an
already-redacted frame, obtains exactly one in-memory grant, deletes the
ciphertext before invoking the native consumer, clears recovered bytes on every
path, and terminally revokes on invariant or consumer failure. Consumer grants,
receipts, pixel frames, and decrypted bytes are not public and cannot cross the
XPC protocol. Its dedicated directory is exclusively
leased for the sink lifetime; a two-pass descriptor-scoped startup scan cleans
only exact crash orphans and fails without deletion on unknown or suspicious
entries, while authenticated corruption is consumed and removed before error
return. The XPC method list remains exactly session start, readiness, prepare,
and execute, with five-field content-free terminal outcomes; screenshot capture
remains excluded from the production factory. The real Keychain path has not
run. A separate source-bound Henry qualifier killed 10
owned DEBUG Swift test publishers after durable ciphertext publication; all 10
artifacts survived without destructor cleanup and all 10 fresh sink processes
removed the exact orphan. That is real process-kill evidence, but it is not a
production-signed installed XPC or sudden-power-loss test. Signed live validation is
still required for ScreenCaptureKit, session transitions, Accessibility,
CGEvent, Apple Events, and the review-only Shortcut handoff. The exact contract
and limitations are recorded in `docs/austin-native-control-contract.md`.

The Austin XPC boundary now also has an authenticated, content-free readiness
method. It consumes the next session capability sequence and runs only public,
non-prompting preflights from the adapter identity: `AXIsProcessTrusted`,
`CGPreflightScreenCaptureAccess`, `CGPreflightPostEventAccess`, and
`AEDeterminePermissionToAutomateTarget` with `askUserIfNeeded` false for the two
reviewed Activate adapters. The reply has a closed canonical vocabulary and
reports `control_protocol_enabled` from the sealed production factory. The
current missing-profile foundation reports false. The readiness method cannot
grant a TCC permission, send an Apple Event, capture a frame, post an event, or
enable the dispatcher. The capability sequence is consumed under the XPC session lock,
but the potentially slow OS preflights run after that lock is released and
under one process-wide readiness lease. Concurrent signed-relay probes fail
closed as busy, and relay XPC timeouts use monotonic dispatch deadlines. Swift
6.4 toolchains use the public macOS 27 picker `isAvailable` signal; older
supported SDKs require macOS 15.2 so the selected window or display identity
can be bound and revalidated before capture. macOS 14 through 15.1 now report
the safe picker path unavailable rather than equating API presence with exact
target authority. No production-
signed invocation has run.

The 2026-07-21 local-test adds a separate `austin-readiness-probe` executable
that is built and ad-hoc signed only for non-prompting developer diagnostics.
It observed the closed permission vocabulary on the current Xcode 27 host while
keeping `control_protocol_enabled` false, and the enclosing test still recorded
no installation, persistent runtime writes, activation, or TCC prompts. The
probe is deliberately excluded from the staged application bundle. Because
macOS may attribute a command-line child to its responsible terminal process,
these observations do not establish the identity or TCC state of a future
Developer ID installation and do not advance HARD-060 through HARD-064 or the
production portion of HARD-070.

A read-only distribution inspection on 2026-07-20 confirms the staged arm64 app
has hardened runtime and the exact sandbox/application-group entitlements, but
it is ad hoc signed, has no TeamIdentifier, is rejected by Gatekeeper, and this
host reports zero valid code-signing identities. A manual-only protected
`native-hardening` workflow now turns the first external gate into an executable
cell on a dedicated one-job ephemeral self-hosted Apple-Silicon signing runner.
The job targets an `algo-cli-signing` organization runner group whose access
must be restricted to this exact workflow on protected `main`; labels alone are
not treated as authorization.
It requires the protected default branch, non-bypassable environment approval
with self-review disabled, pre-provisioned Developer ID and `notarytool`
Keychain identities, an independently configured public-key digest, two
notarization rounds, Gatekeeper assessment, provenance attestation, and bounded
cleanup. The dispatch exposes no inputs. All eight trust anchors come only from
the protected environment: the two signing identities, Team ID, notary profile,
extension origin, public key, public-key digest, and runner-attestation digest.
The package version is read from source and the build number is the GitHub run
number. The workflow rejects any ref other than the protected default branch
before checkout, requires the exact `algo-cli-signing-ephemeral` label, and
performs a clean credential-free checkout.

Before a setup action or dependency installation can run, a system-Python
preflight verifies a digest-pinned, root-owned canonical
`AdaAustinSigningRunner.json`. The attestation is bound to this repository,
workflow, protected `main` ref, runner name and exact labels, current boot
session, a declared image digest, a maximum 24-hour provisioning lifetime, and
a running root launchd log forwarder with at least 30 days declared retention.
It also requires the exact GitHub source commit, a clean checkout with no
untracked files, and no submodules. Dependency caching remains disabled. This
raises the entry bar but does not prove that the provisioner supplied a clean
image, that forwarded logs arrived externally, or that the runner was destroyed
afterward. The workflow neither installs the package nor touches TCC, and it has
not executed, so none of those live gates are claimed.

The separate `henry_austin_signing_provisioner.py` is the only admitted local
manifest writer. It is an offline administrator tool, not a workflow step. Its
production entry point requires effective UID 0 on Apple Silicon macOS, rejects
the presence of `GITHUB_ACTIONS`, opens only the fixed root-owned
`/Library/Application Support` boundary, creates or verifies a nonwritable
`AlgoCLI` directory through directory descriptors, and publishes the canonical
manifest with an exclusive `0600` temporary file, durable completion, and a
no-overwrite hard link. It
requires an operator-supplied image digest, runner name, 30-to-365-day declared
log retention, and no more than 24 hours of lifetime. It reads the current boot
session and requires the system log forwarder to be running before publication.
The schema-v2 manifest pins numeric GitHub repository ID `1297752684` rather
than the mutable owner/name. The signed job still binds the GitHub-supplied
owner/name, workflow ref, workflow SHA, job, server, and API endpoint, allowing
a reviewed organization transfer without accepting a same-named substitute.
A typical disposable-host invocation is:

```sh
sudo /usr/bin/python3 scripts/henry_austin_signing_provisioner.py \
  --runner-name Austin-Ephemeral-1 \
  --image-digest "$AUSTIN_IMAGE_DIGEST" \
  --log-retention-days 30 \
  --lifetime-hours 24
```

Success prints only the manifest digest and structural limitations. A separate
environment administrator must place that exact digest into the protected
`AUSTIN_RUNNER_ATTESTATION_SHA256` secret; the tool never contacts GitHub or
sets a secret. It refuses to replace an existing manifest. The disposable host
must be rebuilt rather than reusing or refreshing the trust record. Source and
negative tests prove schema compatibility, canonical output, secure ownership,
symlink refusal, partial-write cleanup, and no-overwrite behavior. They do not
prove the supplied image digest, external receipt of forwarded logs, one-job
execution, or post-job host destruction.

Ada's post-job lifecycle verifier now supplies the local receipt contract. A
protected external controller must pin the authority-file digest and current
dispatch binding, then collect three distinct Ed25519-signed observations from
the GitHub controller, external log sink, and host provider. Exact common-run
binding, GitHub success and runner absence, delivered `Runner_` and `Worker_`
logs, provider-confirmed host/storage destruction, released network identity,
bounded retention, temporal ordering, freshness, and anti-replay checks are all
fail closed. Descriptor-relative reads reject symlink ancestors, hard links,
insecure modes, noncanonical JSON/Base64URL, duplicate keys, key substitution,
and mixed-run receipt sets. The production authorities file is intentionally
unconfigured and the current evidence report is blocked. Forty-nine direct
tests and the 190-test transfer-safe signing/package matrix pass locally, but fixture
signatures are not external evidence and do not close M7 or M8.

Henry's offline lifecycle-authority preflight closes the hand-authored trust-
root gap without changing production state. It accepts only canonical Ed25519
public-key PEM files outside the repository, verifies separately supplied raw-
key digests, enforces three distinct keys and IDs, and writes one private,
canonical, fsynced, no-overwrite `AdaAustinLifecycleAuthorities.json` candidate
outside the repository. It explicitly rejects private-key material and reports
`activation_eligible` false. Independent review, installation, protected digest
pinning, and proof that each external authority controls its corresponding
private key remain mandatory.

Henry's read-only GitHub readiness gate first binds numeric repository ID
`1297752684`, then cross-checks organization ownership; a
selected-repository organization runner group restricted to this exact workflow
on protected `main`; the protected default branch; exact environment policy;
independent reviewer posture; exact eight-secret inventory; API-reported
ephemeral runner state and labels; workflow registration; and byte identity for
the complete remote/local file-backed workflow inventory. GitHub-reported
Dependabot platform rows are classified separately from repository workflow
files, and any unrecognized platform path blocks readiness. The gate separately
rejects any other file-backed workflow that can select a self-hosted or signing
runner, reference a protected trust anchor, use an ambiguous environment, or
select anything except an explicitly bounded GitHub-hosted runner matrix. The
runner-group restriction is the authoritative alternate-workflow boundary.

The fresh 2026-07-20 live report passes 4 of 17 checks and blocks 13. The pinned
repository ID, protected `main`, the two recognized Dependabot platform rows, and absence of alternate
signing authority in the two remote workflows pass. Organization ownership,
the `native-hardening` environment and all eight secrets, the workflow-
restricted runner group, an eligible ephemeral runner, Austin workflow
registration/source identity, the four-file workflow inventory, and the
protected signing trust contract are absent. The public repository is currently
owned by the `Seabass-up` user account, so an organization runner group cannot
exist there. Those prerequisites must be created deliberately before the
protected cell can run; their absence is a real external blocker, not passing
negative evidence. A repository-level or persistent signer is not an allowed
fallback.

Ada's local native claim database now has a bounded, rollback-resistant
retention foundation. A SQLite transaction advances a durable clock high-water,
rejects timestamps outside a five-second reordering window, rejects an object
that is no longer live at the newest observed time, removes only rows expired at
that high-water, and commits the new replay row plus exact namespace counts as
one unit. Expiry indexes avoid full-table scans; each namespace is capped at
32,768 live claims and the database connection enforces a 32 MiB page ceiling,
one-page WAL checkpoints, a 1 MiB retained-journal target, and a 2 MiB cache
target. Startup migrates existing rows into the state record, runs SQLite's
quick check, validates counts/high-water, and rejects unsafe database or sidecar
shapes.

This closes the unsafe wall-clock-deletion design gap locally; it does not close
M6. The proof depends on Samuel verification and finite signed lifetimes before
Ada admission, non-reuse of authority-issued UUIDs, and an untampered same-user
database. A large forward clock jump followed by rollback intentionally causes
a durable fail-closed state. The DEBUG-only Austin/Ada probe now covers 100 real
process kills at five transaction checkpoints across both claim namespaces: 80
pre-commit attempts roll back fully, 20 post-commit attempts remain durable,
every surviving claim rejects replay, and the release binary contains no
crash-hook marker. A separate
256-claim expiry-churn cell leaves one live row and remains below the hard page
ceiling. Before production enablement, the installed build still needs real
sudden-power-loss and long-duration/high-capacity qualification. The required
quiesced authority-rotation recovery sequence now has an unwired, externally
anchored, signed three-phase state machine with rollback, interruption,
concurrency, deterministic anchor identity, path-identity, expiry, and downgrade
adversarial tests. It cannot
perform or qualify the actual bundle/key/database mutation until a signed
installer can revoke and replace the sealed Samuel authority.

### M7 — Packaging, readiness, receipts, and supply chain

Deliverables:

- Readiness states: not installed, installed/unpaired, paired/missing permissions, ready idle, connected/no grant, active, degraded, and version mismatch.
- Doctor verifies path, signature, team/designated requirement, hardened runtime, Gatekeeper, notarization, protocol range, each permission, extension install/live/tab-grant distinctions, and isolation availability.
- Keychain-protected signing/pairing keys and signed tamper-evident receipt sequence heads.
- Locked/pinned dependencies, SBOM, provenance, binary/extension/native-host signature checks, exact entitlement allowlists, uninstall cleanup, and protocol compatibility policy.

Current foundation state (2026-07-19): Arthur defines a closed readiness
schema for all eight required states and rejects missing, extra,
wrong-category, contradictory, or forged derived evidence. The read-only doctor
can inspect an exact Austin bundle for its layout, signature, team identity,
designated requirement, hardened runtime, Gatekeeper, stapled notarization,
exact entitlements, and LaunchAgent definition. It deliberately reports live
XPC and TCC checks as unknown by default and reports the Chrome and managed-
browser surfaces as not installed; filesystem presence is never promoted to a
live or permissioned state. An explicit `--live-native-probe` mode is available
only after every static trust prerequisite passes. It launches the installed
relay with a sanitized environment, requires mutual signed XPC authentication,
parses an exact canonical content-free permission reply, and rechecks the relay
file identity after execution. Missing, denied, not-determined, unavailable,
malformed, noncanonical, timed-out, or protocol-disabled states remain distinct
and fail closed. This path does not prompt or grant permission, and it has not
run against a production-signed installation.

Ada receipt schema 2 now signs a random journal identity, contiguous sequence,
and previous-receipt digest into every content-free receipt. Full-chain reads
detect signature, row/blob, link, interior-deletion, and cross-journal errors.
A caller-supplied signed head retained outside the SQLite file detects tail
rollback. Grace now provides a bounded compare-and-set receipt-head store in a
recognized OS credential backend. Anchored Ada journals automatically verify
and advance the signed external head after the SQLite commit, recover only from
an authenticated prefix, and reject missing, malformed, foreign, divergent, or
rolled-back anchors. David's production factory requires the anchor while
disabled simulators remain explicitly unanchored. Adversarial tests cover write
interruption, lost-anchor, tail rollback, tampering, journal substitution, CAS
contention, and a reader racing a legitimate external advance. An ephemeral
macOS Keychain create/reread/CAS/delete probe passed without printing or
retaining value material.

The log remains tamper-evident rather than immutable. The Python process still
owns the credential item, deletion of both local evidence stores by the same
privileged principal is not preventable, and native/remote anchor composition
is not installed. HARD-071 is verified narrowly for signed, tamper-evident
receipts and externally checkable sequence heads without an immutability claim.
The stronger production-native identity, remote retention, and real power-loss
lifecycle remain separate M7/M8 release gates.

The Python lock, build backend, build/audit tools, Rust toolchain, Go toolchain,
and external GitHub Actions are exact-version or commit pinned. CI now requires
Python dependency audit, Rust format/clippy/tests/RustSec audit, Go race tests,
at least 60 percent gateway coverage, vet, and `govulncheck`. The first live Go
scan found the workflow's Go 1.26.4 standard library affected by GO-2026-5856;
the workflow and gateway module now pin Go 1.26.5, ordinary module-local Go
commands self-select that fixed toolchain, and the rescan reports no known
vulnerabilities. A deterministic CycloneDX 1.5 artifact records the locked
runtime resolution, not every unconstrained installer resolution and not
dependencies embedded in the wheel. Wheel/sdist checksums, Twine validation,
public-content scan, and isolated wheel smoke pass locally. Release provenance
and SBOM attestations are prepared with commit-pinned actions but cannot be
generated locally and cannot run while the release freeze is active.

Strict non-editable validation also found and fixed three CI-only failure
classes: tests importing developer scripts as an installed package, curated
memory tests assuming the checkout resource root, and coverage measuring the
checkout while tests executed the installed wheel. The settled matrix passes
2,870 tests and records 66.76 percent branch coverage against the package that
was actually imported. A later exact-install pass also caught a stale locally
installed build before test collection: it lacked the fresh-postcondition API
and Henry qualification module even though the checkout contained both. The
Oliver isolated parity gate now runs under `python -I`, rejects source
shadowing, and compares every source-owned package file byte-for-byte while
also rejecting missing or stale installed Python modules. The settled local
install matches all 268 source-owned files, including 257 Python files, with
no missing, divergent, or unexpected Python paths. CI and release jobs force
reinstallation before running the same parity gate.

The Oliver uninstall foundation now closes the locally implementable deletion
boundary without pretending an installer exists. A canonical signed inventory
authorizes only `Algo CLI Control.app`, its exact user LaunchAgent, and the
stable-Chrome `com.algo_cli.neon.json` native-host manifest. Dry-run plans bind
current presence and launchd state to a typed confirmation digest. Execution
uses no shell, glob, recursive removal, privilege escalation, or wildcard
credential deletion; it revalidates content and inode identity, removes leaves
before parents, preserves user state by default, and emits a signed structural
receipt for completed or unknown outcomes. A separately confirmed private-state
purge uses exact Keychain label fingerprints and compare-and-delete, leaving
unknown items untouched. Thirteen focused adversarial tests pass, including
partial-tree reconciliation and pre-mutation rejection of changed, extra,
linked, symlinked, writable, running, or non-removable objects. The work also
fixed the doctor's stale `/Applications/Algo CLI.app` default and its
LaunchAgent-schema mismatch.

Austin's fail-closed release packager now requires exact same-team Developer ID
Application and Installer identities, a named `notarytool` Keychain profile,
strict version/build/origin inputs, nested inside-out hardened-runtime signing,
exact entitlements and designated requirements, and two notarization/stapling/
Gatekeeper rounds: first the app, then the signed flat package containing that
already-stapled app. It requires Accepted zero-issue notarization logs, verifies
the package signature with `pkgutil`, never accepts raw credentials or a shell,
never overwrites output, and emits content-free Ada release evidence. The local
machine has no qualifying identity, so only its fail-closed path and simulated
command contract have executed.

The packaging audit also corrected three pre-qualification defects: Installer
identity discovery no longer relies on the code-signing policy, application
identity discovery cannot fall back to a weaker generic policy, and the shared
Austin stage is serialized under a bounded native lock. Child output is bounded
while it is produced, the disabled-native release key is copied through a
pinned descriptor into private staging, notary logs are size-bounded before
decode, and package verification now requires the exact Installer chain line,
trusted distribution status, and trusted timestamp described by Apple's
current notarization guidance.

The packager also requires the requested package version to equal the source
version and the 32-byte sealed authority key to match an independently retained
SHA-256 identity. It rechecks that digest at config validation, immediately
before staging, and inside the signed app both before and after notarization;
the content-free Ada release evidence records the same digest. This prevents a
same-size substituted authority key from silently becoming the signed control
root. The protected workflow and these checks are enforcement source until a
real signed run supplies hosted evidence.

The package installs only the signed app. The explicit non-root
`algo-cli-control-install` finalizer then verifies Developer ID, hardened
runtime, exact entitlements, Gatekeeper, and stapled notarization; creates only
the inert per-user LaunchAgent and stable-Chrome native-host manifest with
descriptor-relative no-overwrite writes; verifies the app again; and atomically
publishes the signed Ada inventory and independent per-user authority. It never
loads the agent, requests TCC, pairs Chrome, or enables native control. The
release-sealed disabled-native key is intentionally different from the per-user
inventory signer because a notarized public app cannot be customized per user
without invalidating its signature. Inventory schema 2 binds semantic version
and build number and rejects downgrade evidence replacement while allowing
exact-version reinstall under the ordinary freshness rules.

Grace now persists a signed, revisioned, closed-schema Ada credential registry.
It contains every fixed label, registers a dynamic receipt-anchor tombstone
before that credential write, and provides an atomic label/fingerprint snapshot
under one global lease. Missing, forged, malformed, wrong-authority, or partial
registries fail closed. Fresh initialization is allowed only when all known
fixed labels are absent in injected test backends. Production initialization
now requires a nonce-bound census from the exact Developer-ID-signed,
empty-entitlement `austin-credential-migrator`: it enumerates the complete
`algo-cli-runtime` Keychain service, returns labels plus value digests only, and
is identity-checked before and after its bounded invocation. The census digest
is committed into the signed registry, and unexpected, replayed, stale, changed,
or partial namespaces fail closed. Source, fixture, and ad-hoc identity-rejection
tests pass; the production-signed disposable-Keychain lifecycle has not run, so
private-state purge is still not production-qualified. Runtime-only cleanup
remains independently representable and preserves private state.

Oliver now durably writes a signed Ada recovery authorization before the first
uninstall mutation, deletes the registry second-to-last and the signing key
last, and resumes only the original confirmed ID sets with fresh content-digest
checks. Before deleting the final signing key, it pre-signs the exact completion
receipt and atomically publishes a signed `commit_ready` record. Recovery then
requires every earlier runtime and credential surface to remain absent and
either deletes the still-matching final key or verifies that it is already
absent before returning that receipt. It never needs to mint a post-deletion
signature. Fault injection covers every instrumented runtime, commit, and purge
boundary; all of them reconcile locally without a false receipt. The record is
tamper-evident, not filesystem-immutable, and production power-loss behavior on
a signed disposable installation remains a release gate.

M7 remains in progress. Production Developer ID signing, notarization,
Gatekeeper, finalizer-issued Ada inventory/authority files, an official team ID
pinned into the distributed finalizer, execution of authenticated migration on
empty and pre-registry disposable Keychain namespaces, an executed signed
install-finalize-upgrade-downgrade-
rejection-reinstall-uninstall lifecycle, live extension/native-host
identity, live browser pairing/connection/tab-grant evidence, live Austin
XPC/TCC evidence, crash/power-loss uninstall qualification, and executed GitHub
attestations are still required. Hosted execution of the new macOS native CI
cell is also pending. The current machine remains untouched because
the absent signed inventory blocks the uninstall CLI before mutation.

### M8 — Adversarial verification and efficiency

Hard gates:

- Zero generated privileged specs.
- Zero protected actions without required confirmation.
- Zero stale-target mutations in 10,000 race-injected actions.
- Zero automatic retries after unknown mutation outcomes.
- Zero raw secrets or protected content in logs, telemetry, receipts, or memory.
- Zero default-profile, cookie, storage, password, incognito, internal-page, or private-network access.
- Zero arbitrary program acceptance.
- Zero crashes/OOMs in 100,000 malformed protocol frames.
- 100% stale/expired/revoked/wrong-target permit rejection.
- 100% successful mutations backed by a fresh postcondition.
- At least 95% supported managed-browser completion and 90% selected-Chrome completion.
- At least 90% semantic-route usage, 50% token reduction, and 70% screenshot reduction versus a screenshot-only baseline.
- Browser security update lag no more than 72 hours.

Benchmark evidence must separate cold and warm runs, use frozen fixture digests, randomized order/seeds, at least five repetitions for public claims, independent checkers, and publish denominators/confidence intervals. “Zero observed in N trials” must never be marketed as zero risk.

Current local qualification state (2026-07-20): the disabled Henry M8 runner
records nine locally provable gates as passing and five production-browser
gates as blocked. It observed zero dispatches and zero mutations in 10,000
claim-to-dispatch target races; zero automatic redispatches in 1,000 unknown
outcomes; 1,000 of 1,000 malformed typed programs rejected; 1,000 of 1,000
privacy canaries absent from audit and telemetry projections; 100,000 of
100,000 malformed frames rejected with zero uncaught crashes; and 1,000 of
1,000 successful simulator mutations backed by a newer target-bound
postcondition. Five local efficiency repetitions measured 78.812 percent
median schema reduction and 96.644 percent typed-program reduction. Wilson
intervals and p50/p95 local timings are retained in
`hardening/grace-m8-local-qualification.json`.

The current artifact is bound to source digest
`sha256:57f4fc6c4d3dad81c65f94e6452e451c57dc79d066d39bb7c4efc7b603cffaab`
and artifact digest
`sha256:200e6b5e1f3dab8175a54f5f7b6ef226d6b14754cd9199ae04cbfb28198b8c80`.
Its stale-target cell measured p50 4.497 ms and p95 4.901 ms; its fresh-
postcondition cell measured p50 5.945 ms and p95 6.301 ms. All 89 native tests
pass in ordinary, AddressSanitizer, and ThreadSanitizer matrices, and all 3,131
installed-package Python tests pass in 50.02 seconds at 66.95 percent branch
coverage. The 50-file mypy contract, full Ruff lint, native release/static
audit, Rust/Go/website/distribution tests, and Python/Rust/Go/npm dependency
audits also pass. These remain finite local measurements, not live signed-
browser, signer, external-lifecycle, or TCC proof.

The M8 fingerprint now covers every C, header, and Swift source and test in the
native package, including the sealed Thomas production factory and Alice
capture consumer, key, exclusive lease, crash-orphan recovery foundation, the
sealed Isaac consumer pipeline, and the unqualified rectangle-only Vision
redaction candidate.
It also binds the Henry/Alice process-kill qualifier, its source-bound 10-trial
artifact, adversarial output/identity tests, the root-owned boot-bound Austin
signer-attestation verifier, the offline no-overwrite Austin manifest
provisioner, the organization workflow-restricted runner-group gate, complete
workflow inventory classification, and their adversarial tests.
A regression test fails if a future native file is omitted. Henry, its David
fuzzer child, and the Oliver uninstall source wrapper also resolve the checkout
from their own path, so supported project-environment invocations no longer
depend on the caller's working directory or an editable installation. M8
generation also excludes only its post-write
artifact-currency assertion while producing a replacement report; the complete
external test gate runs that assertion after publication. This removes the
previous circular stale-artifact failure without weakening the checked-in
evidence gate.

The hosted Boron path now cross-binds every live result to the browser image
ID, broker image ID, broker code digest, and authoritative release-evidence
digest from the one qualified build. A tag or evidence substitution rejects
before launch and again during report construction. The source-bound M8 corpus
also includes the freeze gate and its adversarial tests, so malformed ledger
evidence cannot be hidden behind an otherwise current browser digest. This is
stronger local integrity evidence, but the remote repository still exposes only
the pre-hardening CI and release workflows; the hosted cell has not executed.

The qualification pass exposed and fixed a real runtime defect: verified
reconciliation previously accepted a structurally valid adapter assertion
without requiring a newer observation. David now requires a fresh snapshot
and binds its digest into the signed receipt; missing, stale, wrong-target, or
mismatched evidence remains unknown.

M8 remains in progress. The current Linux stable image now passes the corrected
update-lag calculation locally. The new five-session hosted Boron cell is a
repeatability and containment qualification, not a task benchmark: it does not
label its fresh containers as cold/warm model runs and cannot fill completion,
semantic-route, token, screenshot, or selected-Chrome metrics. Managed-browser completion,
selected-Chrome completion, semantic-route and screenshot reductions, and live
profile/network containment cannot pass until the exact M5/M7 production
topology exists. The report is explicitly not eligible for public claims, and
finite zero-failure samples are not described as zero risk.

The unreleased Agent runtime candidate adds a separate source-bound,
model-free M8 cell. An immutable Run Contract now binds approval semantics,
block prompts, tools, mutation scope, workspace identity, verifiers, recovery,
and resource ceilings before execution. A private hash-chained journal records
content-free intent/outcome checkpoints and rejects both byte tampering and
semantically impossible event ordering. Resume accepts only the last
contiguous verified block boundary and fails closed on authority, prompt,
context, workspace, or uncertain-mutation drift. One provider-neutral loop
balances tool calls/results, a provenance-labeled context broker enforces a
single token budget, and multi-signal routing preserves the highest observed
risk. The `nathan-agent-runtime-hardening-v1` artifact records deterministic
correctness probes plus p50/p95 contract, context, and durable
checkpoint/resume latency. It remains local candidate evidence and does not
claim model quality, production crash/power-loss behavior, or superiority over
another harness.

### M9 — Requirement audit and controlled freeze lift

For each ledger item, inspect the authoritative current code, test, runtime, signed artifact, package, permission state, or benchmark output. Mark uncertain, missing, indirect, or narrow evidence as not verified.

The active David gate now rejects malformed evidence objects, unknown evidence
kinds, noncanonical SHA-256 and UTC fields, control characters, unsafe or
missing milestone evidence paths, duplicate authorizations, and milestone /
requirement status contradictions. This prevents a structurally dishonest M9
lift, but it does not substitute for rerunning and independently inspecting
every authoritative gate below.

Arthur's deterministic M9 auditor adds the semantic completion layer. It pins
the exact 42-requirement identity and evidence-class contract, requires every
verified external browser, signing, TCC, installation, and benchmark item to
carry digest-bound `hosted pass:`, `signed pass:`, or `production pass:`
evidence, and distinguishes `blocked`, `failed`, `ready_for_lift`, and `passed`.
Its Ada report is an exact canonical projection of the complete current ledger;
any ledger edit makes the stored report stale and fails the freeze workflow.
The M8 source digest includes both the auditor and its adversarial tests.

This audit does not authenticate GitHub, Apple, Chrome, runner, signing,
permission, or benchmark state by itself. Those external systems remain the
authoritative sources. The report remains public-claim-ineligible even after a
structural pass; it is a freeze-lift gate, not marketing evidence. The active
freeze workflow deliberately expects `blocked`. Reaching `ready_for_lift`
therefore stops the normal workflow until a separately reviewed lift change
updates the gate, ledger, and freeze controls together.

Current audited state (2026-07-20): 28 of 42 requirements are verified, 14 are
blocked, and zero are failed. The Ada report SHA-256 is
`3bf2bfafe0dc4655e6074897ec68ed614eb9a935ad1ad65652dcfb6747f00cd6`;
it binds ledger digest
`sha256:6181749d2adb98614c74a7493d56ec54f6e4b9644cb36eae61895caf36df4882`.
This is a useful integrity checkpoint, not evidence that the 14 external and
production-runtime requirements are complete.

The freeze can be lifted only when:

- Every requirement is `verified` with evidence.
- Full cross-platform Python, Rust, Go, website, package, security, fuzz, native signing, macOS, and Chrome matrices pass.
- No required work remains.
- A dedicated audit change sets the freeze to lifted and explains every gate.
- The lift itself passes the freeze gate and receives an explicit audited commit.

## 6. Evidence format

Each verified ledger item must record:

```json
{
  "kind": "test|command|artifact|runtime|audit|benchmark",
  "path_or_command": "authoritative source",
  "digest": "sha256 when applicable",
  "result": "pass",
  "timestamp": "RFC3339",
  "scope": "what this proves",
  "limitations": "what it does not prove"
}
```

Passing a test is evidence only for the behavior the test actually exercises. A checklist, plan, code comment, or plausible implementation is not completion evidence.
