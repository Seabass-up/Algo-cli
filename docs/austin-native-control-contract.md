# Austin native control contract

Status: disabled hardening foundation. It is not installed, paired, registered,
or reachable from Algo CLI's normal action registry. It is not release-ready.

## Security objective

Austin is the narrow macOS authority boundary for future computer use. The
networked Python/model process may propose a finite action, but it must never
inherit Accessibility, Screen Recording, Apple Events, or event-posting
authority. A signed native component must independently authenticate its peer,
validate the exact signed grant and one-use permit, revalidate the live target,
perform at most one reviewed action, and return only a structural outcome.

```text
networked Python/model
        |
        | signed control.prepare, then target-bound control.execute
        v
App-Sandboxed Austin relay (no network entitlement)
        |
        | authenticated Mach/XPC + per-connection capability
        v
user LaunchAgent Austin TCC adapter (no root, TCP listener, or credentials)
        |
        +-- Swift Ed25519 preparation + execution verification
        +-- durable one-use preparation and permit claims
        +-- fixed-copy confirmation + ephemeral target/action binding
        +-- AX / capture / fixed Apple Event / bounded CGEvent adapter
```

The TCC adapter is intentionally unsandboxed because a general Accessibility
controller cannot operate from App Sandbox. It has no network entitlement,
network framework, socket API, listener, model credential, subprocess, dynamic
script, clipboard, event tap, keyboard injection, or silent permission-request
API. This is a reviewed and audited networkless design, not an OS-enforced
network sandbox for the TCC adapter. The relay provides the OS App Sandbox
boundary on the networked side.

## Identity and transport

- The release requirement is an exact Developer ID team, exact bundle
  identifier, Developer ID Application certificate chain, and hardened runtime.
- The relay and adapter apply mutual XPC code-signing requirements.
- The LaunchAgent is a same-user Aqua agent in the user's graphical bootstrap
  namespace. It is not root and has no `Sockets` or persistent `KeepAlive` job.
- The adapter validates same UID, a local non-root/nonremote graphical Security
  Session, PID, and process start time. PID alone is never an identity.
- Each XPC connection gets a 32-byte CSPRNG capability, strict sequence, five
  minute maximum lifetime, and 64-call maximum. Any capability or sequence
  error invalidates the session.
- The wire is canonical integer-only JSON, at most 65,536 bytes, with exact
  schemas and bounds on depth, items, strings, and safe integers.
- Release startup derives the enclosing `.app` from the absolute helper path,
  validates the complete sealed bundle, then reads exactly one nonwritable,
  single-link, 32-byte Ed25519 public key resource. A loose helper, relative
  path, bad bundle signature, writable key, wrong size, or invalid layout fails
  closed.

The DEBUG-only ad-hoc requirement and empty relay entitlement profile exist
only for the local XPC behavior probe. They are excluded from production
evidence and cannot satisfy signing, App Sandbox, TCC, notarization, or
Gatekeeper requirements.

## Authority and permit lifecycle

Target discovery is a separate authority phase. Samuel signs a canonical
`control.prepare` object with a 60-second maximum window and exact preparation
ID, future execution request ID, subject, operation, data class, native route,
reviewed selector, and structural arguments. It contains no target, snapshot,
grant, permit, or native-derived element ID. Austin verifies that signature,
rejects every route/operation/selector combination outside a seven-case native
matrix, confirms once through fixed native copy, and durably claims the
preparation ID before discovery so concurrent or restarted replay cannot show a
second prompt. Text input and picker-scoped capture are not admitted through
this CLI preparation path.

Thomas then derives the native binding and returns only the opaque target,
opaque element when required, exact snapshot fields, and structural geometry
needed to construct the future request. The Python consumer accepts only the
canonical reply schema, cross-binds every signed preparation argument to the
native final arguments, enforces the shorter route-specific binding lifetime,
and constructs a request containing exactly that target, snapshot, operation,
and route. A second, independently signed
`control.execute` envelope remains mandatory. Before the execution permit can
be claimed, the dispatcher rechecks the preparation's request ID, subject,
operation, data class, route, target generation, snapshot/fence, exact canonical
argument digest, binding lifetime, and one-use state. Adapter bind methods
consume an atomic, preparation-scoped confirmation lease; they no longer accept
caller-supplied action-confirmation booleans. A prepared binding therefore
cannot be swapped to another request, subject, operation, route, argument set,
or permit.

The adapter independently decodes the Python `ControlEnvelope` and checks:

- protocol and message type;
- the pinned policy digest and exact Ed25519 authority key;
- grant and permit signatures with domain separation;
- subject, target, target kind, epoch, revision, fence, snapshot, sequence,
  operation, effects, data class, route, byte bounds, and expiry cross-binding;
- a narrower native route table than the Python policy;
- no upload route and no generic activate-over-coordinate fallback.

Readiness is evaluated before the durable claim. A missing, consumed, expired,
wrong-target, or geometry-mismatched native binding cannot burn a valid permit.
Once a ready action starts, its permit is claimed with `BEGIN IMMEDIATE` in a
private `0600` SQLite database under a `0700` directory. The insert is durable
across process restart and exactly one of concurrent replay attempts can win.
Timeout, disconnect, and framework ambiguity are terminal
`unknown_outcome`; they are never automatically retried.

## Route rules

| Route | Native rule | Success rule |
|---|---|---|
| Accessibility | HMAC-opaque element binding, maximum 5 seconds and one use; PID/start, bundle, window, focus, role, subrole, enabled state, session, screen, modal, and sensitivity are rechecked | Only a fresh target-bound postcondition can succeed |
| Text input | No AX text injection in this foundation | Always native handoff; secure, auth, payment, unknown input, keyboard-layout, and IME risks cannot be bypassed |
| Screen capture | Picker-scoped and persistent-programmatic leases use distinct issuance paths; one frame, maximum 3 seconds | Pixels are locally redacted before the sink; raw frame storage, telemetry, memory, and XPC return are prohibited |
| Apple Events | Only `activate_finder` and `activate_system_settings`; exact bundle targets and the Core Suite Activate event | Frontmost bundle must freshly match; timeout is unknown and one-shot |
| Shortcuts | One fixed `open-shortcut` review handoff; no automatic runner, input, clipboard, callback URL, output, subprocess, script, or caller-supplied name | Native confirmation and one-shot binding are required; opening the editor returns handoff-required, an uncertain open is unknown, and the shortcut is never executed automatically |
| Coordinates | Exact point, logical viewport, pixel dimensions, scale, display, process start, and focus binding; single display; maximum 2 seconds | Post permission is preflight-only; exactly mouse-down and mouse-up are posted; generic clicks remain unknown without a semantic postcondition |

The adapter and coordinator in-memory binding registries prune expired/consumed entries and have
hard capacity limits. Active capture or coordinate bindings cannot be silently
replaced with a different mode or point.

## Outcome and privacy contract

Execution replies contain only protocol version, status, reason code,
operation, and route. They never contain application names, bundle IDs, target
IDs, element IDs, coordinates, text, pixels, paths, selectors, grant bodies, or
permit bodies. Preparation replies are the narrow exception: they return the
opaque target and element identifiers plus exact structural snapshot/viewport
fields required to construct the separately authorized execution request. They
still contain no application or bundle identity, role, label, text, pixel,
path, selector, grant, or permit body. The terminal states are `succeeded`,
`denied`, `handoff_required`, `unknown_outcome`, and `failed`.

An AX cannot-complete error, Apple Event timeout, uncertain CGEvent post, or
unverifiable mutation is never represented as success. Generic AX button
presses and coordinate clicks therefore remain unknown unless a fresh semantic
postcondition proves the requested effect.

## Current evidence

The current checkout demonstrates:

- DEBUG and RELEASE Swift compilation;
- 89 Swift policy, wire, authority, identity, lifecycle, replay, privacy, and
  adversarial tests in the ordinary build and under AddressSanitizer and
  ThreadSanitizer;
- staged exact-entitlement and signed-bundle audit;
- no Network framework or socket symbols in the staged binaries;
- source rejection for network APIs, subprocesses, dynamic scripts, silent TCC
  prompts, Input Monitoring, keyboard injection, clipboard, and model
  credentials;
- a bounded ScreenCaptureKit one-frame backend, a one-shot system-picker source,
  and an encrypted short-lived Alice sink for already-redacted pixels;
- a content-free classifier preflight before confirmation, post-acquisition
  classification before redaction/persistence, an internal rectangle-only
  Vision candidate with full-frame fallback, and a sealed module-local Alice
  consumer pipeline that deletes ciphertext before native consumption;
- fixed-copy native confirmation and a one-second presence lease invalidated by
  public session-resign and screen-sleep notifications, without claiming that
  those notifications are a lock-state oracle;
- a fixed review-only Shortcut editor handoff with no automatic execution,
  input, clipboard, callback, or output path;
- a Python/Swift-compatible target-free preparation schema, exact Ed25519 test
  vector, durable concurrent replay claim, fixed-confirmation Thomas
  coordinator, canonical target-bound Python consumer, exact
  future-execution cross-binding, and guarded dispatcher;
- a real user LaunchAgent/Mach/XPC DEBUG probe with mutual identifier checks,
  session capability, and zero TCP/UDP sockets on the adapter process.

The staged app now also contains a standalone `neon-native-host` invocation
guard with an empty entitlement set and no network framework or socket symbol.
It derives the enclosing app from its absolute helper path, validates the
complete sealed bundle, opens the sealed extension origin with `O_NOFOLLOW`,
requires one regular single-link resource that the caller cannot write, reads
it through a pinned descriptor with before/after state checks, revalidates the
bundle, and accepts exactly one matching Chrome extension-origin argument.
The guard deliberately writes no native-messaging frame: all invocations exit
78, mismatched origins report `extension_origin_rejected`, and the exact sealed
origin reports `protocol_disabled`. Three staged negative probes verify zero
stdout. This closes the missing-executable/package-layout bug without claiming
that browser pairing or the Python observation protocol is connected.

The staged app also contains an empty-entitlement
`austin-credential-migrator`. It accepts one canonical, nonce-bound request on
standard input, derives its own static code identity, and rejects an ad-hoc,
missing, or wrong identifier/team identity before issuing any Keychain query.
Only the exact `algo-cli-runtime` generic-password service is queried, with
`kSecMatchLimitAll` and synchronizable-any semantics. The bounded canonical
reply contains labels and SHA-256 value fingerprints, never credential values.
It distinguishes the registry record, fixed labels, dynamic receipt-head
anchors, and unexpected labels. The Python finalizer verifies the complete app
identity both before and after execution, checks a five-minute freshness window
and the one-use nonce, and commits the census digest into the newly signed Ada
registry. The census payload is not independently signed; its authority comes
from the exact Developer-ID-signed executable observed on both sides of the
bounded process call. The local ad-hoc negative probe proves only that identity
rejection happens before the Keychain query.

The work also found and fixed two staging/runtime defects: macOS `base64 -D`
silently produced a 30-byte key from unpadded base64url, and a helper launched
from `Contents/Helpers` could not use `Bundle.main` to locate the sealed key.
The build and staged audit now reject either condition.

The M7 Arthur doctor can now inspect an exact installed bundle for path/layout,
signature, Developer ID team and designated requirement, hardened runtime,
Gatekeeper, stapled notarization, exact entitlement allowlists, and the finite
LaunchAgent definition. It intentionally reports XPC peer authentication and
each TCC permission as unknown by default; it does not infer readiness from
files or plist presence. Its explicit `--live-native-probe` path executes only
after every static trust prerequisite passes. The signed relay opens a mutually
authenticated XPC session and consumes one capability sequence to request an
exact content-free snapshot from the adapter identity. Accessibility, Screen
Recording, post-event, and both reviewed Apple Event permissions use public
preflight APIs that never request or prompt for access. The system picker and
the deliberately disabled dispatcher are reported separately. On toolchains
that expose the macOS 27 API, picker readiness uses the public `isAvailable`
signal. Older supported SDKs report ready only on macOS 15.2 or newer, where
Apple exposes the exact included-window or included-display identity needed to
bind the one-use filter; macOS 14 through 15.1 fail closed before picker UI.
The adapter consumes the authenticated sequence under its session lock, then
releases that lock before OS preflights so a slow framework response cannot
starve connection invalidation or later calls. One process-wide readiness lease
rejects concurrent signed-relay fan-out, and the relay's XPC waits use monotonic
dispatch deadlines rather than wall time. The doctor
sanitizes `DYLD_*` and test-only Austin variables, validates canonical JSON with
duplicate-key and type-confusion rejection, limits stdout to 4 KiB while the
relay is running, rejects any stderr, terminates the isolated probe process
group on a bound or timeout violation, and verifies the relay identity is
unchanged after execution. No production-signed live result exists yet.

## Signed installation inventory and bounded uninstall

Austin now has a fail-closed release packager, but this checkout has not produced
a production artifact. The packager requires exact Developer ID Application and
Developer ID Installer identities from the same ten-character team, an existing
named `notarytool` Keychain profile, a strict semantic version and numeric build,
one exact extension origin, and one 32-byte disabled-native authority public
key plus its independently retained SHA-256 identity. The requested version
must equal the package source version. It accepts no raw notarization credentials
and never invokes a shell.
Application identity discovery uses the `codesigning` policy; Installer
identity discovery uses the unfiltered valid-identity list because Apple
Installer certificates are not code-signing identities. The fixed shared
native staging tree is protected by a bounded `lockf` lease, command output is
terminated at its byte bound while the child is running, and the caller's
release key is descriptor-read and copied into the private release staging
directory before any build command uses it. The key digest is checked during
configuration, again immediately before that copy, and against the sealed app
resource before and after notarization. The same digest is retained in
`AdaAustinReleaseEvidence.json`.

The packager stages and signs every nested executable inside-out with a secure
timestamp and hardened runtime, verifies exact identifiers, team, certificate
chain, designated requirements, and entitlement allowlists, and runs the native
package audit. It then performs two distinct notarization rounds: the signed app
is zipped, submitted, required to have an Accepted zero-issue log, stapled,
validated, and assessed by Gatekeeper before it is placed in a Developer ID
Installer-signed flat package; the package is then independently submitted,
log-checked, stapled, validated, signature-checked with `pkgutil`, and assessed
by Gatekeeper. Output is a new directory containing the package and a
content-free `AdaAustinReleaseEvidence.json`; an existing output path is never
overwritten.

The manual `henry-austin-signing-qualification` workflow is restricted to a
protected ref, the approval-gated `native-hardening` environment, and a
dedicated one-job ephemeral self-hosted Apple-Silicon runner with the exact
`algo-cli-signing-ephemeral` label inside an `algo-cli-signing` organization
runner group. That group must allow only this repository and only this exact
workflow pinned to protected `main`; a repository-level runner or label-only
boundary is forbidden. An explicit `github.ref_protected` guard
and exact-default-branch guard reject before checkout, and public-key
preparation independently checks the unmodifiable `GITHUB_REF_PROTECTED` runner
variable before key materialization. The dispatch has no caller inputs. Its
eight protected environment secrets are the Application and Installer
identities, Team ID, `notarytool` Keychain profile, extension origin, disabled
authority public key, independent public-key digest, and runner-attestation
digest. Version comes from the source package and build number from
`github.run_number`; neither is caller supplied. Checkout is clean and
credential-free; dependency caches are disabled. Only the public key is
materialized, under the runner temp directory with exclusive mode `0600`. The
workflow attests the package and structural evidence, retains them for seven
days, and removes transient key, package, and staged-build material.

Immediately after checkout and before setup actions or dependency installation,
`/usr/bin/python3 scripts/henry_austin_signing_runner.py` verifies a canonical,
root-owned manifest at
`/Library/Application Support/AlgoCLI/AdaAustinSigningRunner.json` against its
protected digest. The maximum 24-hour attestation binds this repository,
workflow, protected `main` ref, exact runner identity and labels, current boot
session, declared image digest, and system launchd log-forwarder label. The host
probes require the exact GitHub commit, a completely clean and untracked-free
checkout, zero submodules, and a running log-forwarder process with declared
retention of 30 to 365 days. The preflight remains content-free and explicitly
does not prove certificate custody, image construction, external log receipt,
runner destruction, notarization, or artifact correctness.

`scripts/henry_austin_signing_provisioner.py` is the matching offline writer.
The production entry point runs only as effective UID 0 on Apple Silicon macOS
outside GitHub Actions. It accepts an explicit runner name, declared image
digest, 30-to-365-day log-retention period, and at most 24-hour lifetime; reads
the live boot-session UUID; and requires the root launchd log forwarder to be
running. It confines output to the fixed root-owned Application Support
directory, rejects symlinked or group/world-writable boundaries, writes a
canonical root-owned `0600` temporary file, fsyncs it, changes the completed
inode to final read-only-for-nonroot mode, and uses a hard-link commit that
cannot overwrite an existing manifest. Fault cleanup targets only the file
identity created by that invocation. Its JSON output contains the resulting
digest and limitations but never echoes the runner name, image digest, or boot
UUID.

The digest must be moved out of band by a separate environment administrator
into `AUSTIN_RUNNER_ATTESTATION_SHA256`; the provisioner does not call GitHub,
create an image-provenance statement, or authenticate the operator-supplied
image digest. A disposable runner is rebuilt rather than refreshing its
manifest. The manifest pins GitHub repository ID `1297752684`, not the mutable
owner/name, so an organization transfer cannot silently select a different
repository or strand the signer on the retired personal-account name. The
current owner/name remains bound by GitHub's non-overridable default variables
and by the signed post-job receipt set. The current source and negative tests establish compatibility with
the workflow preflight and safe publication behavior only.

`scripts/ada_austin_signing_lifecycle_receipt.py` now defines the missing
post-job evidence boundary. It runs outside the signing host and accepts three
canonical, independently signed Ed25519 receipts: a GitHub-controller job
observation, an external-log-sink delivery observation, and a host-provider
destruction observation. The three protected public keys and key IDs must be
distinct. All receipts repeat and sign the same closed binding: receipt ID,
repository name and numeric ID, workflow, protected ref, source commit, run and attempt, job and
runner IDs, runner name/group/labels, preflight-attestation digest, image
digest, and boot-session UUID. The caller must separately pin both the
authority-file digest and the expected current-dispatch binding digest through
`ADA_AUSTIN_LIFECYCLE_AUTHORITIES_SHA256` and
`ADA_AUSTIN_LIFECYCLE_BINDING_SHA256`. Supplying a key file and receipts in the
same invocation is therefore insufficient.

The GitHub receipt requires the current REST API version, matching workflow and
job names, matching head SHA and branch, a completed successful job, a
`workflow_job` completed-delivery ID, artifact/package/release-evidence
digests, and a post-job observation that the runner registration is absent.
Absence is deliberately not interpreted as host destruction. The log-sink
receipt separately requires an archive digest, at least one `Runner_` log and
one `Worker_` log, receipt timestamps, and 30-to-365-day retention. The provider
receipt separately requires digests for the provider instance and destroy
operation plus destroyed host/storage state and released network identity.
The verifier cross-checks all bindings and signatures, enforces a 95-minute job
ceiling, six-hour lifecycle ceiling, five-minute clock-skew allowance, 24-hour
receipt freshness, completion/log/destroy/observation ordering, and current-run
anti-replay binding.

Authority and receipt files must be canonical newline-terminated JSON, singly
linked, owner-matched, and not group/world writable. They are opened by
descriptor-relative, no-follow traversal so a symlinked leaf or ancestor cannot
redirect verification. The content-free result exposes only digests, status,
reason, and limitations; it never repeats runner, provider, run, delivery, or
key material. Optional evidence publication is private, fsynced, confined to
the hardening directory, and no-overwrite.

`scripts/henry_austin_lifecycle_authority_preflight.py` prepares the missing
authority candidate without activating it. It accepts three absolute,
off-repository files containing canonical SubjectPublicKeyInfo PEM for distinct
Ed25519 public keys plus independently supplied raw-public-key SHA-256 pins and
distinct key IDs. Private-key PEM, non-Ed25519 keys, noncanonical PEM, duplicate
keys or IDs, symlinked or multiply linked files, writable inputs, CI execution,
and owner drift fail closed. The output path must be an existing secure
off-repository directory and the exact memory-style filename
`AdaAustinLifecycleAuthorities.json`; publication is `0600`, fsynced, and
no-overwrite. The content-free result returns only the candidate digest, count,
status, and limitations with `activation_eligible` false.

The candidate is not the production trust root. A separate administrator must
review its current repository name and pinned numeric repository ID, install it
as the production Ada authority file through an audited change, and pin the
candidate digest in the protected external controller. The tool never reads a
private key, calls GitHub, edits the repository sentinel, provisions an
authority, or proves that the three authority services control the matching
private keys.

```bash
python scripts/henry_austin_lifecycle_authority_preflight.py \
  --output /secure/off-repository/AdaAustinLifecycleAuthorities.json \
  --repository ORG/Algo-cli \
  --github-controller-key-id github-controller-v1 \
  --github-controller-public-key /secure/controller-public.pem \
  --github-controller-public-key-sha256 sha256:... \
  --log-sink-key-id external-log-sink-v1 \
  --log-sink-public-key /secure/log-sink-public.pem \
  --log-sink-public-key-sha256 sha256:... \
  --host-provider-key-id host-provider-v1 \
  --host-provider-public-key /secure/host-provider-public.pem \
  --host-provider-public-key-sha256 sha256:...
```

Each raw-public-key digest must arrive through the authority's independently
authenticated channel. Computing a digest from the same untrusted file proves
format consistency, not authority identity.

Production remains honestly blocked. The protected authority file is explicitly
`unconfigured` with digest
`sha256:d121ad7bd04846372f1951357b1937ce07f9fd5fac3e808f08d24a0989fc783f`,
and the recorded probe is `blocked` rather than a fixture pass. The local
49-test adversarial corpus proves parser, signature, key-substitution,
signature-swapping, mixed-run, replay, time-order, log-retention, teardown,
Base64URL, duplicate-key, file-identity, symlink, privacy, and immutable-output
behavior. It does not produce any external receipt or close the real lifecycle
gate.

### Post-job receipt wire contract

Every receipt has exactly these top-level members:

```json
{
  "authority_key_id": "protected-key-id",
  "binding": {},
  "kind": "github_job|external_log|host_destroyed",
  "observation": {},
  "schema_version": 2,
  "signature": "canonical-unpadded-base64url"
}
```

The signed message is the ASCII domain below followed by canonical compact JSON
and one LF for the complete receipt with only `signature` removed:

- `algo-cli:austin-lifecycle:github-job:v2\0`
- `algo-cli:austin-lifecycle:external-log:v2\0`
- `algo-cli:austin-lifecycle:host-destroyed:v2\0`

The common `binding` has exactly `receipt_id`, `repository`, `repository_id`, `workflow`, `ref`,
`source_commit`, `run_id`, `run_attempt`, `job_id`, `runner_id`, `runner_name`,
`runner_group`, `runner_labels`, `runner_attestation_digest`, `image_digest`,
and `boot_session_uuid`. The current-dispatch pin is SHA-256 over the same
canonical binding object and LF. Receipt IDs are lowercase canonical UUIDv4;
delivery and boot IDs are lowercase canonical UUID strings; hashes use
lowercase `sha256:` form; booleans cannot substitute for integer IDs.

The GitHub observation has exactly `api_version`, `workflow_name`, `job_name`,
`status`, `conclusion`, `workflow_job_action`, `workflow_job_delivery_id`,
`head_branch`, `head_sha`, `job_started_at`, `job_completed_at`, `observed_at`,
`runner_registration_state`, `package_digest`, `release_evidence_digest`, and
`artifact_attestation_digest`. The external-log observation has exactly
`archive_digest`, `runner_log_count`, `worker_log_count`, `first_event_at`,
`last_event_at`, `received_at`, `receipt_issued_at`, and `retention_until`. The
host-provider observation has exactly `provider_instance_digest`,
`destroy_operation_digest`, `destroyed_at`, `receipt_issued_at`, `host_state`,
`storage_state`, and `network_identity_state`. No raw provider instance ID, log
URI, artifact content, certificate, token, or secret belongs in a receipt.

The configured schema-v2 authority file pins numeric repository ID
`1297752684`, the then-current owner/name, and the workflow/job/ref/runner scope.
This permits a reviewed organization transfer while rejecting a same-named
substitute repository. It contains exactly the `github_controller`, `log_sink`, and
`host_provider` public-key records. Each record contains `key_id`, a canonical
32-byte Ed25519 `public_key_base64url`, and the raw-key `public_key_sha256`.
Private keys must remain in the three external trust domains and must never be
placed on the signer or in this repository.

`scripts/henry_github_hardening_readiness.py` performs a read-only pre-dispatch
control-plane check. It requires the pinned numeric repository ID and
organization ownership; an organization runner
group that selects only this public repository and is restricted to this exact
workflow on protected `main`; the default branch to be protected; an exact
non-bypassable, protected-branches-only environment with independent approval
and self-review disabled; exactly the eight expected environment secrets; one
online, idle, API-reported ephemeral runner with the exact labels; one active
signing workflow; byte identity for every registered remote workflow and every
local file-backed workflow; the no-input, environment-only signing trust contract; and the
absence of any alternate workflow path to a self-hosted/signing runner,
protected trust anchor, or ambiguous environment. GitHub's two current
Dependabot `dynamic/...` registry rows are recognized as platform-managed rather
than repository files; any unknown platform path blocks. Non-signing workflows
are limited to literal GitHub-hosted runner labels or an explicitly bounded
GitHub-hosted OS matrix. It intentionally does not claim runner-image
cleanliness, certificate custody, log delivery, or post-job host destruction.
This follows GitHub's
current protected-ref variable, environment protection, and one-job ephemeral-
runner guidance; GitHub's external-log-forwarding recommendation remains an
operational requirement, not a source-level proof.

The fresh live 2026-07-20 preflight passes 4 of 17 checks and blocks 13. It
confirms the pinned repository ID, protected `main`, recognizes both GitHub-managed Dependabot rows, and
finds no alternate signing authority in the two registered remote workflows.
The repository is public and owned by a user rather than an organization, so it
cannot provide the required workflow-restricted organization runner group. The
`native-hardening` environment, its eight secrets, an eligible ephemeral
runner, the Austin workflow, exact four-file remote source inventory, and the
signing trust contract are also absent. The workflow has not executed. It is a
signed-package qualification only, not installation, XPC, TCC, browser pairing,
or permission-lifecycle evidence. Provisioning those protected prerequisites is
mandatory before execution; no fallback to a repository-level, unprotected, or
persistent runner, raw certificate secret, cached dependency environment, or
ad-hoc signing is allowed.

The package installs only the already-notarized app. It does not use a root
postinstall script to write into an inferred user's home, Keychain, or TCC
database. The installed Python entry point `algo-cli-control-install` is an
explicit non-root, current-user finalizer. It verifies the installed app before
mutation and reads the exact sealed extension origin. If no signed credential
registry exists, it invokes the exact signed credential migrator and initializes
the registry only from a complete, nonce-bound census; an existing registry is
verified and reused. It then creates only the inert LaunchAgent plist and
stable-Chrome native-host manifest with descriptor-relative no-overwrite writes,
verifies the app again, and atomically publishes signed Ada install evidence. It
does not bootstrap the agent, grant or request TCC permissions, pair a browser,
or enable the disabled native protocol. Exact retries are idempotent; an
existing conflicting file blocks without overwrite.

Oliver defines the corresponding uninstall boundary.
The canonical app name is `Algo CLI Control.app`; this also fixes the former
doctor default of `/Applications/Algo CLI.app`, which could never have matched
the staged bundle. The only removable runtime surfaces are:

- `/Applications/Algo CLI Control.app`;
- the exact per-user LaunchAgent plist for
  `group.com.algo-cli.control.austin.tcc-adapter`; and
- the exact stable-Chrome per-user native-host manifest
  `com.algo_cli.neon.json`.

Chrome for Testing and Chromium have different native-host locations and are
not silently added to the production inventory. The native-host manifest has a
single exact extension origin, an absolute host path inside the signed app, and
the finite `stdio` schema required by Chrome. The installed LaunchAgent schema
now matches the staged definition, including Aqua session restriction,
interactive process type, and throttle interval.

An installer must capture every regular file and directory under those three
roots into a canonical, bounded inventory. The inventory records only logical
surface-relative names, modes, ownership, sizes, content digests, the exact
team/extension/authority identities, and content-free credential fingerprints.
It is authenticated by a domain-separated Ed25519 signature and verified
against an independently retained 32-byte public key. No inventory means no
uninstall authority. Capture additionally requires all five declared native
executables, exact app identity, a sealed disabled-native authority key, and a
sealed `NeonAllowedOrigin.txt` matching the manifest origin. The release-sealed
native key and per-user install-inventory signer are deliberately independent:
a public notarized app cannot embed a different per-user public key without
invalidating its signature. The complete app tree, including the sealed native
key, is still hashed into the signed install inventory.

The Oliver post-install evidence publisher now verifies the exact Developer ID
team and identifiers, hardened runtime, designated requirements, entitlement
allowlists, Gatekeeper acceptance, and stapled notarization before it captures
the live tree. It publishes `AdaInstallAuthority.bin` and the signed canonical
`AdaInstallInventory.json` under a private directory with a bounded lock and
atomic write/fsync/replace/fsync ordering. Valid evidence is idempotent; only a
newer distinct install may supersede it, and corrupt or tampered evidence is
never overwritten. Inventory schema 2 signs the app version and build number.
A lower semantic-version/build tuple is rejected without replacing existing
evidence; exact-version reinstall is allowed only under the normal fresh-install
identity and time rules. The explicit finalizer invokes this publisher, but no
production-signed package/finalizer lifecycle has run, so source and fixture
existence are not install-lifecycle evidence.

The uninstaller is dry-run first. Its plan is deterministic and binds the
signed inventory, every currently present entry, exact credential entries, and
the LaunchAgent state. Execution requires both that plan digest and its derived
typed confirmation phrase. The installed entry point is
`algo-cli-control-uninstall`; its source-checkout wrapper has identical
behavior. Before mutation it rejects changed, extra, linked,
symlinked, unsupported, writable, wrong-owner, running, or non-removable
objects. It never uses a glob, shell, privilege escalation, broad process kill,
recursive deletion, or wildcard Keychain operation. Files are re-opened with
`O_NOFOLLOW`, rehashed, inode-checked, and unlinked relative to pinned parent
directory descriptors. Directories are removed leaf-first only with `rmdir`, so
an unexpected child stops cleanup rather than being swept away.

Normal uninstall preserves memory, encrypted artifacts, audit history, and all
Keychain state. The separate private-state purge requires a signed complete
label inventory, digest compare-and-delete for each exact item, and its own plan
confirmation. Unknown service items survive. Interrupted filesystem cleanup is
idempotently reconcilable because missing signed entries are allowed while any
new or changed entry remains blocking. Once confirmed, every terminal path
that retains signing authority returns a content-free signed receipt; ambiguity
is `unknown_outcome`, never a false success or blind retry.

Before its first mutation, the installed uninstaller now atomically publishes
an owner-only, content-free, signed Ada write-ahead record named for the install
ID. It binds the confirmed plan, complete original entry/credential ID sets,
launch state, inventory, mode, and authority. Recovery never replans a broader
surface: it may remove only still-present objects from that signed set, rereads
every file or credential digest, and can continue after the credential registry
has already been deleted. The registry is deleted second-to-last and the control
signing key last. A runtime-only completed receipt is embedded in a monotonic
terminal record with write/fsync/replace/fsync ordering; a dry run does not
create the record or its lock file. Local fault injection now interrupts every
instrumented boundary. Every runtime-only boundary reconciles to a verified
terminal record.

Private purge uses a two-phase terminal transition. After all earlier signed
objects are absent, the still-live control signer pre-signs the exact completion
receipt and the store durably publishes a signed `commit_ready` record. Only
then may compare-and-delete remove the final signer. A restart verifies the
record and receipt with the independently retained install public key, requires
the LaunchAgent, processes, runtime tree, and every earlier credential to remain
absent, and deletes the final signer only if its digest still matches. If it is
already absent, the pre-signed receipt is returned; no post-deletion signature
is manufactured. Reappeared files or credentials, a changed signer, tampering,
or a non-purge `commit_ready` record fail closed. Every instrumented purge
boundary now reconciles locally. This closes the former two signer/receipt
windows without claiming that the owner-controlled recovery file is immutable.

Grace now owns a finite, closed-schema Ada credential-label registry stored in
the recognized OS credential backend. The registry is signed by the control
authority, revisioned, bounded to 256 labels and 64 KiB, contains every fixed
Algo credential label, and registers dynamic receipt-anchor tombstones before
the corresponding credential write. Registry changes and complete
label/fingerprint snapshots share one global inventory lease, so install
capture cannot combine labels and fingerprints from different registry states.
Missing, malformed, forged, duplicated, out-of-order, wrong-service, or
wrong-authority registry state blocks a complete inventory and private purge.

Production fresh initialization is allowed only from the exact signed native
census. The census must cover the complete service namespace, contain no
unexpected labels or pre-existing registry, and match every reread fixed or
dynamic label fingerprint before the signed registry is written. This closes
the former generic-keyring blind spot where a stranded dynamic receipt anchor
could be omitted. Direct fixed-label probing remains available only with an
injected test backend and cannot initialize a production namespace. Source,
fixture, and ad-hoc rejection tests now cover empty and legacy namespaces, but
no Developer-ID-signed migration has executed against a disposable production
Keychain. Private-state purge therefore remains disabled as production behavior
until that lifecycle passes. Runtime-only uninstall remains independent and
preserves all private state.

Focused tests currently cover canonical round-trip and tampering,
dry-run stability, exact runtime removal, private-state preservation and purge,
wrong confirmation, changed/extra/symlink/hardlink/writable/non-removable
objects, partial-tree reconciliation, LaunchAgent failure, and content-free CLI
failure, plus post-install publication, interruption recovery, identity
requirements, signed-registry tamper/race/failure handling, complete credential
snapshots, nonce replay/freshness rejection, unexpected and dynamic labels,
sealed origin binding, bounded helper output, pre/post identity verification,
idempotent finalization, upgrade, downgrade rejection, signed recovery-record
tampering and concurrent publication, and power loss at every instrumented
runtime/purge boundary. This is local foundation evidence only.
The expected retained files
`AdaInstallInventory.json` and `AdaInstallAuthority.bin` do not exist because no
production package/finalizer has been authorized or run; the live uninstall CLI
therefore blocks without changing this machine. A signed install/finalize/
upgrade/downgrade-rejection/reinstall/uninstall lifecycle and crash/power-loss
qualification remain required.

## Explicit blockers and residual work

M6 remains in progress because this machine has no valid Developer ID signing
identity. The following are not yet demonstrated:

- Developer ID signing, production App-Sandbox launch, mutual team/bundle XPC
  authentication, notarization, stapling, or Gatekeeper assessment;
- an official Developer ID team pinned into the distributed finalizer contract;
  the current explicit `--team-id` input is a foundation interface, not an
  acceptable public-release trust anchor;
- an installed, moved, upgraded, rolled-back, reinstalled, and uninstalled app
  with stable TCC identity and finalizer-issued Ada evidence;
- permission denial, grant, revocation, regrant, sleep, screen lock, fast-user
  switching, display hotplug, and multiple-user tests on signed builds;
- execution of the authenticated readiness probe from the production-signed
  installed relay across denied, not-determined, granted, revoked, and
  unavailable-target permission phases; source and fixture tests do not
  establish any current TCC state;
- activation of the new production Thomas factory in a signed distribution.
  The normal LaunchAgent now reads one exact canonical resource from the sealed
  app, rejects malformed or unsupported policy, and can assemble AX, reviewed
  Apple Event, review-only Shortcut, and bounded coordinate adapters. A missing
  resource remains the disabled foundation, and no enabled production resource
  has been built or qualified;
- production qualification and activation of the redaction-classification
  authority. Classifier availability is checked before fixed native
  confirmation, while pixel-dependent classification correctly runs only after
  bounded frame acquisition and before redaction or persistence. The internal
  Vision candidate uses text-rectangle and face-rectangle requests only, never
  OCR or recognized strings; it caps and merges detector regions and falls back
  to full-frame redaction on private, empty, overloaded, or fragmented output.
  Fixture and source audits pass, but no live accuracy corpus, false-negative
  bound, display-hotplug matrix, or production activation exists. Frame capture
  validates all redaction bounds and aggregate work before mutation, caps total
  redaction work at one frame's pixel count, and rejects overlapping-region amplification.
  Picker filters are now
  locally bound to exactly one window or display ID plus exact geometry on
  macOS 15.2 or newer, carried in the one-use lease, and revalidated before
  ScreenCaptureKit starts; macOS 14 through 15.1 fail before presenting the
  picker because Apple does not expose those identities there. No live picker
  callback or display-hotplug race has been qualified. Alice now has a local-
  device, non-synchronizing, non-interactive
  Keychain load-or-create factory in the adapter's default code-signing access
  group plus a one-use in-memory-capability consumer that deletes ciphertext
  before returning already-redacted pixels. The sink holds a nonblocking
  exclusive lease on its exact-0700 directory, validates every entry before
  startup mutation, deletes only revalidated exact owner-private crash orphans,
  preserves and rejects unknown or suspicious entries, scrubs consumed
  capability material, and deletes authenticated corruption before returning
  failure. A separate bounded qualifier verifies 10 owned DEBUG Swift test
  process kills: every ciphertext survives the killed publisher and every fresh
  sink removes the exact orphan. This is not signed installed-XPC or sudden-
  power-loss evidence. A sealed module-local pipeline now encrypts only an
  already-redacted frame, consumes exactly one in-memory grant, deletes the
  ciphertext before invoking the native consumer, clears recovered bytes, and
  terminally revokes on failure. Capability APIs are internal, and source audit
  rejects any grant, receipt, pixel, or decrypted-frame reference in the exact
  four-method XPC boundary. Screenshot execution remains excluded from the
  production Thomas factory, and the real Keychain path has not run;
- signed live validation of ScreenCaptureKit and the fixed review-only Shortcut
  handoff;
- signed live session-transition testing. The concrete AX and CGEvent backends
  use only a fresh one-second confirmation lease plus public workspace
  notifications; no public lock-state claim is made;
- execution of the authenticated credential migration from a production-signed
  installed app against empty, legacy, unexpected-label, replay, and changed-
  during-census disposable Keychain cases;
- production qualification of Ada's rollback-resistant replay retention. The
  local foundation now persists a transactional clock high-water mark, rejects
  timestamps more than five seconds below its durable admission floor, requires
  every reordered object to remain live beyond the high-water mark, and only
  then deletes claims whose signed expiry is at or below that mark. Permit and
  preparation namespaces each fail closed at 32,768 live rows; expiry indexes,
  a 32 MiB SQLite page ceiling, a 1 MiB WAL retention target, one-page automatic
  checkpoints, a 2 MiB cache target, private sidecar checks, startup quick-check,
  and transactional counts bound normal storage and write cost. This proof
  assumes Samuel has already authenticated the object and enforces its finite
  lifetime, authority-issued UUIDs are never intentionally reused, and the Ada
  database has not been modified by another same-user process. A large forward
  wall-clock jump followed by rollback deliberately leaves control fail closed;
  recovery requires a quiesced, authority-rotating operator procedure rather
  than automatic database deletion. A DEBUG-only probe now executes 100 real
  `SIGKILL` cases across `after_begin`, `after_maintenance`, `after_insert`,
  `after_state`, and `after_commit` for both permit and preparation claims: all
  80 pre-commit cases restore the prior claim/count/high-water state, all 20
  post-commit cases retain the new claim, and every surviving claim rejects
  replay after reopen. The hook marker is
  verified absent from the RELEASE probe binary. This is process-kill evidence,
  not sudden-power-loss evidence; hostile same-user tamper and long-running
  installed-build qualification remain open;
- production crash/power-loss injection through installed XPC and uninstall
  paths, including verification that the pre-signed `commit_ready` transition
  survives real Keychain and filesystem power loss, plus release
  packaging/doctor evidence from the signed artifact.

The LaunchAgent now constructs through `AustinThomasProductionControl`. With no
sealed `AustinNativeControlActivation.json`, that factory returns
`AustinDesktopDispatcher.disabledFoundation()`, no coordinator, and
`control_protocol_enabled: false`. An enabled profile cannot admit screenshot
capture. No permit is consumed and no TCC action is performed by the current
foundation until a signed build contains an exact enabled profile and the live
qualification gates close.

### Ada recovery invariant

Ada never clears or lowers `high_water_ms` automatically. A clock-floor,
integrity, capacity, or page-limit failure must keep the adapter unavailable.
Recovery is permitted only as an installer-owned authority rotation:

1. Quiesce the adapter and relay, prove no live XPC session remains, and retain
   the failed database and sidecars as read-only evidence.
2. Revoke the old Samuel issuer and replace the sealed authority public key, so
   no object accepted by the old replay database can authenticate afterward.
3. Bind the old database digest, old/new authority key IDs, install ID, failure
   reason, and exact replacement paths into a signed, content-free recovery
   receipt before creating a new Ada store.
4. Install the new signed bundle and empty store as one rollback-forbidden
   version transition. The old bundle, authority key, and replay database must
   never be recombined or restored.
5. Re-run Gatekeeper, notarization, designated-requirement, XPC peer, empty/
   legacy/replay, and TCC lifecycle checks before the dispatcher can be enabled.

The unwired `oliver_authority_rotation` foundation now represents this as a
closed three-revision state machine: `authorized`, `commit_ready`, and
`terminal`. The independent control authority signs every revision and chains
it to the prior record. It rejects Samuel key reuse, install-ID reuse, app or
inventory identity reuse, semantic-version downgrade, non-increasing builds,
unbounded authorization windows, and late transitions. Exact normalized paths
are domain-separated and hashed before entering the content-free receipt.

Grace provides a dedicated OS-credential compare-and-set anchor. Oliver
derives its single anchor namespace from the old signed install ID plus old
inventory digest, preventing parallel caller-selected heads for that
installation. It advances that external signed head before replacing its
owner-only cache; an interruption can reconstruct a missing or
one-revision-stale cache from the anchor, while a missing anchor, file-ahead
state, two-revision gap, foreign context, invalid signature, symlink, hardlink,
or insecure directory fails closed. Dynamic anchor labels remain inside Ada's
existing 256-label credential-registry bound. A bounded mutation permit exists
only for an anchored, unexpired `commit_ready` record. The module is not
imported by the runtime registry or Austin adapter, and the dispatcher remains
disabled.

No implementation currently has enough production authority to perform the
bundle/key/database mutation. The state machine is foundation evidence, not a
production recovery run. Deleting or renaming Ada's database by itself remains
explicitly unsafe.

## Verification commands

```sh
swift test --package-path native/austin
swift build --package-path native/austin --configuration release
python scripts/henry_austin_ada_crash_qualification.py --trials 10
script/austin_build_and_run.sh build
script/austin_build_and_run.sh neon-probe
script/austin_build_and_run.sh migration-probe
script/austin_build_and_run.sh readiness-probe
script/austin_build_and_run.sh probe
script/austin_build_and_run.sh local-test
.venv/bin/pytest -q tests/test_austin_native_package.py
.venv/bin/pytest -q tests/test_austin_release_packager.py tests/test_austin_install_finalizer.py tests/test_oliver_control_installation.py tests/test_oliver_control_installer.py tests/test_oliver_authority_rotation.py tests/test_ada_credential_registry.py tests/test_ada_uninstall_recovery.py tests/test_grace_key_store.py
.venv/bin/algo-cli-control-install --help
.venv/bin/algo-cli-control-uninstall
.venv/bin/python scripts/arthur_control_doctor.py --live-native-probe
.venv/bin/python scripts/david_hardening_gate.py
```

The ad-hoc probe is behavior evidence only. Release evidence must use a real
Developer ID identity and the M7 notarization/install/doctor workflow.
`local-test` is the repeatable developer-machine entry point: it runs the
ephemeral XPC probe, the disabled Neon native-host cases, the pre-Keychain
credential-migrator rejection, a non-prompting ad-hoc Apple permission
preflight, and the staged package audit under one build lease. The readiness
probe is deliberately not copied into the application bundle. Its observations
belong only to the ephemeral ad-hoc probe identity and do not establish the TCC
state of a future Developer ID installation. It creates no persistent
LaunchAgent or Chrome manifest, copies nothing to `/Applications`, requests no
TCC permission, and leaves the control protocol disabled.

## Apple release references

- [Customizing the notarization workflow](https://developer.apple.com/documentation/Security/customizing-the-notarization-workflow) defines `notarytool`, Keychain profiles, log review, stapling, and the two notarization rounds required for a custom installer.
- [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues) distinguishes Developer ID Application from Developer ID Installer and documents `codesign` and `pkgutil --check-signature` verification.
- [Packaging Mac software for distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution) documents valid Installer identity discovery and signed package construction.

## GitHub signing-runner references

- [Managing access to self-hosted runners using groups](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access) documents the public-repository warning and exact-workflow runner-group restrictions.
- [Self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners) documents one-job ephemeral runners, autoscaling, and external log-forwarding requirements.
- [REST API endpoints for workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2026-03-10) documents completed job, conclusion, timestamps, source SHA, runner, labels, and runner-group observations.
- [Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads?actionType=completed) documents the completed `workflow_job` lifecycle event used by the external controller.
- [Monitoring and troubleshooting self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot) identifies the `Runner_` and `Worker_` diagnostic logs and the external-preservation requirement for ephemeral runners.
- [Removing self-hosted runners](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/remove-runners) distinguishes an offline registration from removal and machine cleanup.
- [Variables reference](https://docs.github.com/en/actions/reference/workflows-and-actions/variables) defines the non-overridable `GITHUB_REPOSITORY_ID`, owner/name, workflow ref/SHA, job, and server variables used by the runner preflight.
- [Transferring a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/transferring-a-repository) documents owner/name changes, redirects, and the repository state retained across a transfer.
- [Deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) documents reviewer approval and environment-secret availability.
- [Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use) defines the broader GitHub Actions trust-boundary guidance used by this qualification gate.
