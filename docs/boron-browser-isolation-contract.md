# Boron browser isolation contract

Status: M5 hardening foundation, disabled, not registered as an Algo CLI action,
and not a public browser-use claim.

## Security boundary

An ephemeral profile and Chrome command-line flags are not isolation. Public
browsing is eligible only when all of these independently verified layers are
present:

1. A digest-pinned current stable Chrome or Chromium image typed as
   `public_managed`. Chrome for Testing and Playwright images are a disjoint
   `trusted_fixture` type and construction rejects their use as public images.
2. A non-root browser container with a read-only root, no host mounts, no
   published ports or devices, all Linux capabilities dropped, no-new-
   privileges, a deny-by-default seccomp profile, bounded memory/PIDs/CPU, and
   tmpfs-only profile, home, temporary, and download paths.
3. One unique internal Docker bridge containing exactly the browser and its
   egress broker. The browser is attached to no other network. The broker alone
   is dual-homed to a separate egress network. No open CDP TCP or Playwright
   WebSocket endpoint crosses the boundary; the future finite wrapper uses
   Chrome's stdio remote-debugging pipe inside the container.
4. The broker canonicalizes the requested HTTP(S) URL, rejects credentials,
   ambiguous numeric hosts and non-standard ports, resolves every hop, rejects
   the entire answer set if any address is non-public, pins the answer set,
   verifies the actual connected peer, and re-resolves before use. Redirects
   are bounded, HTTPS downgrade is denied, and cross-origin redirects require
   an exact pre-approved origin. `ws` and `wss` requests are not in the finite
   vocabulary.
5. Chrome managed policy blocks all downloads, file-selection dialogs,
   incognito, password saving, sync, extensions, popups, printing, QUIC, and
   internal/file/DevTools navigation. These are defense in depth; Docker and
   broker enforcement remain authoritative.

`algo_cli/boron_browser_isolation.py` builds the fixed topology and verifies
Docker inspect evidence rather than trusting launch arguments.
`algo_cli/xenon_browser_egress.py` owns URL, DNS, redirect, rebinding, and peer
policy. Neither module is imported by the normal action registry.

### Freshness semantics

The public gate measures **security update lag**, not the age of the current
release. Before and after every image build it reads the fixed Google Chrome
VersionHistory Linux stable endpoints over HTTPS with redirects disabled, a
10-second timeout, a 64 KiB response cap, duplicate-key rejection, and an exact
response schema. That observation may be at most five minutes old.

If the pinned image equals the authoritative current version, its update lag is
zero even when the release itself is older than 72 hours. If the pinned image
is behind, lag begins at the newer release's serving timestamp and hard-fails
after 72 hours. A missing, stale, wrong-family, wrong-platform, future,
regressed-version, or release-timestamp-mismatched observation fails closed.
The image still pins the exact package checksum, installed version, image
identity, platform, and release timestamp independently.

On 2026-07-19 the official Linux stable feed and Google package repository both
identified `150.0.7871.128`; VersionHistory recorded its serving start as
`2026-07-16T20:53:47.785001Z`. The local public image therefore attested an
update lag of zero. Chrome for Testing is not consulted or accepted for this
route. The optional arm64 Debian Chromium image remains ineligible because no
authoritative evidence currently proves patch equivalence with that upstream
stable release; its package age is not mislabeled as update lag.

### Current boundary result

On 2026-07-19 a live Docker Desktop probe used a digest-pinned container on a
new internal bridge. Direct public-IP, cloud-metadata, and Docker-host-alias
connections all failed. Inspect evidence showed one network and participant,
non-root execution, a read-only root, the reviewed custom seccomp profile,
dropped capabilities, no-new-privileges, no host bind, no published port, and
bounded PID/memory/private namespaces. Evidence digest:
`sha256:52d47a1e329ec164efcee884029a53963a9e734a8ace1a85be4cec0caf8f0513`.

This proves the container boundary only. It did not start a browser or an
egress broker, so it does not close HARD-050.

Stable macOS Google Chrome 150.0.7871.129 parsed the unpacked MV3 package and produced
a CRX plus key in an automatically deleted temporary directory. This validates
manifest/CSP/package acceptance, not installation, pairing, runtime grant
behavior, or readiness.

### Current source candidate — 2026-08-09

The dated Chrome 150 Linux and macOS results above remain historical evidence
for those exact probes. The current Linux/amd64 source candidate instead pins
Google Chrome `151.0.7922.108`, whose official VersionHistory serving start is
`2026-08-06T20:04:27.459919Z`, and `cryptography` `50.0.0`. These are inspected
source and local-candidate properties, not a new hosted execution result.

The candidate closes several source-level provenance gaps:

- Each browser and broker Dockerfile has its own deny-by-default build-context
  allowlist: the adjacent `.dockerignore` starts with `**` and admits only that
  role's required files.
- The hosted qualifier opens the root, every ancestor, and every declared
  source with no-follow descriptors, validates immutable file identities, and
  constructs one bounded canonical USTAR archive from those held bytes. Both
  images consume those exact archive bytes on Buildx standard input with
  context `-`; no live checkout or staging pathname is available to either
  build.
- Registry evidence keeps the OCI index digest, selected `linux/amd64` platform
  manifest digest, and local image-config digest as distinct identities. The
  full qualification-source digest over the complete declared source set is
  required in both image labels, the build evidence, every live-session row,
  and the reconstructed final report.
- Registry-retrieved OCI indexes, attestation manifests, and in-toto layers are
  fetched by immutable digest and rehashed against their descriptors. The raw
  statements are semantically checked as exactly one BuildKit SLSA v0.2
  predicate and one SPDX 2.2 or 2.3 document with the expected singleton
  subject, source, revision, platform, materials, and packages; build flags,
  copied metadata, and opaque attestation blobs are not accepted as evidence.
- The reachable LLB graph binds the frontend
  `docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32`
  and scanner
  `docker/buildkit-syft-scanner:1.11.0@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68`;
  conflicting or additional frontend/scanner materials fail closed.
- Debian package resolution no longer follows the live `bookworm` mirrors.
  Both Chrome-151 images replace the base image's mirror configuration with
  the exact Debian Snapshot imports `20260712T202631Z` (main and updates) and
  `20260712T194830Z` (security), the last imports before the base image's own
  `20260713T000000Z` source annotation. APT verifies the signed Release
  metadata with the base image's Debian archive keyring; the Dockerfiles also
  require the exact three `InRelease` SHA-256 values, install every requested
  package at an explicit version, and fail unless the final sorted dpkg set is
  exactly 228 entries with digest
  `sha256:8de022828059888145925f8fc14424eb1f8b9a2d01d5bb24abff9d2d0d60a1d9`
  for Boron or 122 entries with digest
  `sha256:945e9057beb01efbdcf89ca6ba002f260eb6bda40f5d535337e7ca7dc6eed640`
  for Xenon. The snapshot bootstrap uses HTTP because the slim base does not
  yet contain CA roots; APT's Release signature and package hashes provide
  authenticity, while a network attacker can still deny availability.
  Chrome and Python artifacts are subsequently fetched over HTTPS and checked
  against their independent SHA-256 pins.
- This stronger resolution boundary is deliberately not described as a
  hermetic or reproducible build. BuildKit does not list bytes downloaded by a
  Dockerfile `RUN` as provenance materials, and the dpkg lock binds package
  names and versions rather than every post-install filesystem byte. Both
  images label `hermetic=false` and `reproducible=false`; the evidence builder
  rejects a BuildKit predicate that says `reproducible=true`. A future
  hermetic claim requires all network artifacts to enter BuildKit as
  digest-addressed materials (or an equivalently attested offline package
  context) and a successful independent bit-for-bit rebuild.
- The candidate GHCR workflow is guarded to a push from the pinned repository
  and repository ID at the exact protected `main` revision. It pins Buildx
  `v0.36.1` by its exact commit and Linux/amd64 release-asset SHA-256
  `48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778`,
  and BuildKit `v0.32.2` by OCI-index digest
  `sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8`,
  uses an isolated Docker configuration directory, resets BuildKit's default
  insecure entitlements, and requires the separately reviewed
  `browser-hardening` GitHub Environment. A no-privilege authority job fails a
  protected-main run unless the repository readiness marker is explicitly set;
  it then verifies the live GitHub Environment API reports protected branches,
  a nonempty required-reviewer rule with self-review prevention, and disabled
  admin bypass. The publisher and attester remain skipped rather than
  auto-creating an unprotected Environment.
- Retained CI artifacts are named with the producing run attempt and passed to
  consumers by immutable artifact ID. Because the hosted report and Sigstore
  certificate both bind the attempt, recovering a failed Boron validation or
  attestation requires **Re-run all jobs**; re-running only a downstream job
  must fail rather than combine attempt-one evidence with attempt-two authority.
- The Buildx binary, Git checkout, index, working tree, and untracked-file state
  are checked before GHCR authentication. After qualification, an explicit
  blocking `docker logout ghcr.io` and strict isolated-config inspection must
  prove that no authentication entry, credential helper, or credential store
  remains before evidence upload. The exact Buildx builder must also be removed
  and proven uninspectable. Action post hooks are only defense in depth.
- Live teardown quiesces every producer before cleanup, verifies exact
  session-and-role ownership, mutates Docker objects only by immutable ID,
  falls back to bounded force removal, and suppresses evidence if any resource
  cannot be proven absent. Report publication likewise remains descriptor-bound
  and rolls back the final path after any late integrity or durability failure.
- Any July artifact produced through the former `command | tee` plus
  `if: always()`/warn-on-missing path is invalidated for qualification. That path
  could retain or attest failure-shaped output without proving that the
  qualification command passed. The candidate requires a successful direct
  report write, exact report reconstruction, error-on-missing upload, digest
  revalidation, and digest-bound attestation.

This section is a source/local candidate record only. No new hosted run has
executed, HARD-050, HARD-052, and HARD-053 remain unverified, and no public
browser-use claim is supported. Authoritative closure still requires evidence
from an approved protected GitHub Environment with working package access, a
real GHCR authenticate/publish/pull/logout lifecycle, five distinct
fresh-ephemeral sessions, independent reconstruction of the retained report,
and the report-digest GitHub attestation.

At the candidate date, the `browser-hardening` Environment and
`BORON_HARDENING_ENVIRONMENT_READY=true` repository marker are not configured,
so the protected-main authority job is intentionally red and no publisher can
run. Configure the Environment first with required reviewers and admin bypass
disabled, restrict it to protected branches, enable self-review prevention,
audit it, then set the marker. The dedicated browser-contract job is also not
yet a required ruleset check; the existing required runtime-quality job
exercises the same tests, but that does not substitute for hosted evidence.

## Target and lifecycle binding

`algo_cli/carbon_browser_binding.py` signs a closed binding over:

- route, paired profile, browser instance, window and tab;
- top-document ID, frame ID, frame-document ID, and canonical-origin digest;
- snapshot ID and monotonic revision;
- opaque element token and exact finite operation set;
- maximum and consumed action counts, issue/expiry times, and fencing token;
- service-worker generation, extension/native versions, and protocol versions;
- selected-tab user-gesture ID and a mandatory non-incognito assertion.

Every action reconstructs and validates even exact-class dataclass instances,
then compares a fresh observation before consuming one action. Navigation,
BFCache, prerender, discarded documents, detached frames, origin changes,
service-worker restart, version skew, fence changes, and stale element tokens
all reject. HMAC signatures use canonical JSON, canonical Base64URL, a key ID,
and constant-time comparison. The key is an injected test authority until M7
provides a Keychain-backed native identity.

## Existing-Chrome selected-tab route

The unpacked Manifest V3 package under
`algo_cli/resources/neon_extension/` has exactly these permissions:

- `activeTab`
- `scripting`
- `nativeMessaging`
- `storage`

It has no host permissions, content scripts, optional host permissions,
debugger, tabs, cookies, history, downloads, webNavigation, webRequest, proxy,
external connection, or web-accessible resources. Incognito is `not_allowed`
and extension pages use packaged scripts only.

Opening the action popup is the user gesture. The service worker injects one
fixed, packaged, top-frame, isolated-world observation function. It returns
only origin, MIME classification, supported-surface classification, and bounded
counts for secure fields, upload controls, canvas, frames, and open shadow
roots. It never returns text, HTML, selectors, cookies, storage, credentials,
or arbitrary JavaScript. Navigation starts, tab closure, native disconnect,
browser restart, or service-worker generation change revoke the session.

M5 deliberately limits this path to `observe` and `handoff`. It contains no
click, type, select, scroll, upload, download, fetch, WebSocket, or CDP action.
The popup says so. Mutation remains blocked until a later audit proves the
end-to-end native authority and postcondition path; this is safer than letting
an apparently minimal extension mutate an uncontrolled user profile.

`algo_cli/neon_browser_native_host.py` implements the bounded stdio framing and
session protocol but is not installed or connected to an executable. It caps
messages at 64 KiB (below
Chrome's documented 1 MiB host-input maximum), rejects duplicate keys, floats,
non-finite numbers, deep/large trees, unexpected fields, wildcard or mismatched
extension origins, version skew, replayed hello/observe/request IDs, incognito,
non-top frames, internal/local/private origins, and non-standard ports. It
returns only opaque profile/origin/document identities and an observe-only
binding.

The Austin staged app now contains a separately compiled Swift
`neon-native-host` invocation guard. Its empty entitlement profile, binary
linkage, sealed app, exact non-writable `NeonAllowedOrigin.txt`, and
caller-origin argument are audited. It intentionally exits 78 with zero stdout
even for the sealed origin because the protocol bridge remains disabled. The
stable-Chrome manifest and signed install inventory are modeled and
cross-bound, but no production installer has placed them. This proves a
fail-closed executable/package boundary; it does not prove extension install,
pairing, connection, observation, or tab grant.

## Explicit edge behavior

| Surface or event | Managed public route | Selected-tab M5 route |
| --- | --- | --- |
| Dialog or popup | Freeze action and hand off; no blind retry | No mutations exist; disconnect/handoff |
| Download | Chrome policy value 3 plus quarantined tmpfs; reject | No downloads permission or mutation |
| Upload/file picker | Disabled policy; future explicit artifact grant counts bytes at selection | Observe count only; no selection |
| Service-worker restart | New generation invalidates binding | Stored generation mismatch disconnects |
| Redirect | Re-resolve, re-pin, bounded, same-origin by default | Any navigation start revokes |
| BFCache/prerender/discard | Non-active lifecycle rejects | Fresh observation/reconnect required |
| Child frame | Exact frame and document binding required | Top frame only |
| Open shadow DOM | Fresh opaque token may be eligible later | Count only |
| Closed shadow DOM | Handoff | Count is unavailable; observation remains non-mutating |
| Canvas/PDF/internal page | Handoff | Classify and remain observe-only; injection failure disconnects |
| Auth/password/passkey/CAPTCHA | Handoff; no secret collection | Classify and remain observe-only |
| User interleaving | Snapshot/fence mismatch rejects | Navigation/tab lifecycle disconnects; no mutations |
| Browser/native version skew | Reject before action | Native hello/observation rejects |
| Broker/browser crash | Unknown effect is never redispatched | Native disconnect revokes |

## Chrome for Testing constraint

Chrome for Testing exists for automation and repeatable testing. Its image must
be pinned by platform digest, fixture digest, browser version, and protocol
version. It may load only trusted local fixtures with networking disabled or
fixture-scoped. It cannot be substituted into the public browsing route, even
if a caller changes an environment variable or command-line flag.

The live trusted-fixture cell used Playwright v1.61.0's arm64 platform manifest
`sha256:35488358a60d3e7eacc4f02e0985e3b46240424d6d7b229732332016e6127508`
and Chromium headless shell 149.0.7827.0. It ran non-root on `--network none`
with a read-only root, all capabilities dropped, no-new-privileges, private
IPC/PID namespaces, resource limits, and the reviewed seccomp digest
`sha256:2a2b908c43d504d2ea6b8cd67bf36cb35a58806b91c9c1320aef727e94a062c6`.
It rendered only the frozen data fixture
`sha256:e223c78883e44788824db7e43c45f4b4f66985013aa2e4084c2ecacd14601d32`
and produced the exact expected DOM. HARD-054 is therefore supported for this
narrow trusted-fixture route; none of this image or evidence qualifies it for
public browsing.

## Verification and known limitations

The initial M5 matrix passes 173 focused tests plus Ruff, JavaScript syntax,
JSON parsing, compileall, the repository freeze gate, and mypy for all four M5
Python modules. It covers private/link-local/multicast/reserved IPv4 and IPv6,
legacy browser IPv4 spellings, mixed DNS answers, rebinding, redirects, peer
pinning, every binding generation, hostile lifecycle state, extension
permissions/source, malformed native frames, replay, and Docker escape-shaped
inspect evidence.

The pass found and fixed these implementation errors before evidence was
claimed:

1. `ipaddress.is_global` classified multicast as global on the active Python
   runtime; multicast and every special-use property are now denied explicitly.
2. The launcher required the exact abstract `Path` type and rejected real
   `PosixPath` objects; it now accepts concrete paths and requires an existing
   file.
3. Native origin checks leaked the egress module's exception type; errors are
   now normalized to the native protocol's content-free error.
4. Docker container image IDs were initially conflated with registry manifest
   digests; the verifier now checks the exact pinned reference/label and the
   observed local image ID as separate identities.
5. Non-root Chrome could not write root-owned `mode=0700` tmpfs mounts; every
   browser tmpfs now carries the browser UID/GID explicitly rather than
   weakening the container to root.
6. With all container capabilities dropped, the Docker-derived seccomp profile
   admitted `chroot` only for a container holding `CAP_SYS_CHROOT`; Chromium's
   unprivileged user-namespace sandbox therefore aborted. The reviewed profile
   now admits the syscall without granting the container capability. Kernel
   namespace capability checks remain authoritative, and `CapDrop=ALL`,
   no-new-privileges, and Chromium sandboxing remain required.

Still unverified:

- the hardened public browser image/wrapper and dual-homed egress broker have
  not completed a hosted live session on native amd64 Linux; the local Apple-
  Silicon gate rejects emulation explicitly after a diagnostic run showed the
  Chrome sandbox fail inside QEMU. The 2026-08-09 candidate source defines a
  pinned `ubuntu-24.04` protected-main GHCR cell with one source-bound build,
  five distinct fresh-ephemeral live sessions, exact image/release/topology
  cross-binding, report reconstruction, and digest-bound attestation. There is
  still no authoritative evidence that a protected Environment admitted the
  job, package access succeeded, GHCR publish/pull/logout completed, the five
  sessions passed, or GitHub attested the reconstructed report;
- no selected-tab package has been installed, paired, and exercised in stable
  Chrome with a production-signed native host and generated manifest;
- WebSocket denial is typed and broker-facing but has not been proven through a
  live TLS/browser path;
- native signing, notarization, TCC, Keychain keys, and installed readiness
  remain unverified; registry SBOM and provenance validation exist in candidate
  source but have no qualifying hosted registry evidence;
- production-browser completion-rate and token/screenshot benchmarks remain
  unexecuted.

HARD-051 and HARD-054 have direct package/process evidence. HARD-050, HARD-052,
and HARD-053 remain unverified until their respective end-to-end live gates
pass. Full Chromium 149 also cleared sandbox startup after the seccomp fix but
its direct `--dump-dom` convenience path hung; the successful fixture gate uses
the purpose-built headless shell and does not hide that full-browser limitation.

## Authoritative references

Accessed 2026-07-19:

- Chrome `activeTab` grant and navigation revocation:
  <https://developer.chrome.com/docs/extensions/develop/concepts/activeTab>
- Chrome scripting API and user-gesture host access:
  <https://developer.chrome.com/docs/extensions/reference/api/scripting>
- Native messaging framing, exact origins, and platform paths:
  <https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging>
- Manifest V3 service-worker lifecycle and loss of global state:
  <https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle>
- Chrome document/frame/navigation lifecycle, BFCache, and prerender behavior:
  <https://developer.chrome.com/docs/extensions/reference/api/webNavigation>
- Incognito `not_allowed`:
  <https://developer.chrome.com/docs/extensions/reference/manifest/incognito>
- Extension CSP and packaged-code restriction:
  <https://developer.chrome.com/docs/extensions/reference/manifest/content-security-policy>
- Extension least-privilege guidance:
  <https://developer.chrome.com/docs/extensions/develop/security-privacy/stay-secure>
- Chrome proxy bypass behavior, including implicit loopback/link-local bypass:
  <https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md>
- Chromium remote-debugging pipe uses inherited stdio instead of TCP:
  <https://chromium.googlesource.com/chromium/src/+/lkgr/content/public/common/content_switches.cc>
- Chrome download policy value 3 blocks all web-triggered downloads:
  <https://chromeenterprise.google/policies/download-restrictions/>
- Chrome URL blocklist scope and limitations:
  <https://chromeenterprise.google/policies/url-blocklist/>
- Chrome for Testing purpose and version APIs:
  <https://developer.chrome.com/blog/chrome-for-testing/>
  and <https://github.com/GoogleChromeLabs/chrome-for-testing>
- Official Chrome VersionHistory API and query semantics:
  <https://developer.chrome.com/docs/web-platform/versionhistory/guide>
- Playwright Docker warning, non-root/seccomp guidance, pinned versions, and
  the v1.61.0 profile from which Algo's reviewed cap-drop-compatible profile is
  derived:
  <https://playwright.dev/docs/docker> and
  <https://github.com/microsoft/playwright/blob/v1.61.0/utils/docker/seccomp_profile.json>

Accessed 2026-08-09:

- Docker Build attestation storage, SLSA provenance fields, and Buildx stdin
  context behavior:
  <https://docs.docker.com/build/metadata/attestations/>,
  <https://docs.docker.com/build/metadata/attestations/slsa-provenance/>,
  <https://docs.docker.com/build/metadata/attestations/slsa-definitions/>, and
  <https://docs.docker.com/reference/cli/docker/buildx/build/>
- Debian Snapshot usage and timestamp semantics:
  <https://snapshot.debian.org/>
- Official Buildx v0.36.1 release and source revision:
  <https://github.com/docker/buildx/releases/tag/v0.36.1> and
  <https://github.com/docker/buildx/tree/v0.36.1>
- Official BuildKit v0.32.2 source used to bind attestation-manifest and
  provenance-material behavior:
  <https://github.com/moby/buildkit/tree/v0.32.2>
