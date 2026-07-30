# David Control Kernel Contract

Status: hardening-only foundation; disabled from the normal Algo CLI action
registry. Protocol version 1 is not a public compatibility promise until M9.

## Boundary

The David kernel accepts one finite typed control request per frame. It never
evaluates model-authored JavaScript, AppleScript, JXA, shell, Python, CSS, CDP,
selectors, SQL, import paths, executable names, or generic programs. Browser
and desktop adapters receive typed dataclasses, not the decoded JSON object.

The wire format is a four-byte unsigned big-endian length followed by one
strict UTF-8 JSON object. The maximum payload is 65,536 bytes. A decoder fails
the connection on a zero/oversized length, invalid UTF-8, BOM, duplicate key,
float/non-finite number, unsafe integer, unpaired surrogate, excessive
depth/item/string size, truncated frame, unknown version/type, missing field,
or extra field. JSON signing uses Algo's integer-only, ASCII-key subset of JCS:
sorted keys, no whitespace, preserved Unicode strings, and no floats.

The implementation is deliberately split at four narrow boundaries:

- `david_control_kernel.py` owns parsing, finite types, local policy, grants,
  permits, signatures, and route choice.
- `ada_control_journal.py` owns private durable claims, revocation, fencing,
  effect transitions, and signed content-free receipts.
- `david_control_runtime.py` owns verify, claim, recheck, dispatch-once, and
  reconcile orchestration across a finite adapter protocol.
- `neon_browser_simulator.py` and `austin_desktop_simulator.py` are hostile
  in-memory lifecycle models. They are not browser or operating-system adapters.

## Closed request language

Every request binds:

- canonical request, session, subject, target, and snapshot identifiers;
- strictly increasing sequence, issue time, and bounded deadline;
- target kind, opaque target ID, epoch, revision, and fencing token;
- snapshot target/generation, observation time, and observation sequence;
- one operation, one data class, exact arguments, requested routes, and output
  byte limit.

Version 1 operations and exact argument objects are:

| Operation | Arguments |
|---|---|
| `observe` | `{}` |
| `activate` | opaque `element_id` |
| `input_text` | opaque `element_id`, bounded `text`, boolean `replace` |
| `select_option` | opaque `element_id`, opaque `option_id` |
| `scroll` | opaque `element_id`, bounded integer `delta_x`, `delta_y` |
| `upload` | opaque `element_id`, UUID `artifact_id`, bounded `byte_count` |
| `coordinate_activate` | bounded `x`, `y`, viewport width and height |
| `handoff` | bounded canonical `reason_code` |

The operation table derives effects and allowed data classes. Requests cannot
declare their own effects. Exact runtime schemas and the exported JSON Schemas
both close objects with `additionalProperties: false`. Exported Draft 2020-12
patterns use an absolute ECMA-262-compatible end guard, so a trailing newline
cannot satisfy a line-end anchor while violating the runtime's full-match rule.

## Authority and policy

An Ed25519 authority signs grants and exact one-use permits over canonical
bytes. Base64URL signatures must use the one canonical unpadded spelling; the
decoder re-encodes and compares before Ed25519 verification. A grant binds
subject, target IDs/kinds, operations, effects, data
classes, routes, issue/expiry, action count, byte limits, policy digest, and
authority key ID. A permit additionally binds the request digest, request and
snapshot IDs, target epoch/revision/fence, sequence, exact derived effects,
route allowlist, byte counts, and a maximum use count of one.

The broker verifies both signatures and revalidates the request against its own
trusted policy. It does not trust permit policy claims as a substitute for
local policy. Expired, not-yet-valid, revoked, replayed, downgraded,
over-limit, wrong-subject, wrong-target, wrong-generation, wrong-policy, and
stale-snapshot requests reject with content-free reason codes.

Route selection is deterministic and least-authority-first:

```text
connector -> shortcut -> apple_event -> dom -> ax -> screenshot
          -> coordinate -> handoff
```

Only the intersection of request, grant, permit, policy, target-kind, and live
adapter routes is considered. Coordinate operations require the coordinate
route; handoff requires the handoff route.

## Durable execution

The SQLite journal uses a private local regular file, WAL, `synchronous=FULL`,
foreign keys, `trusted_schema=OFF`, bounded busy timeout, and `BEGIN IMMEDIATE`
transactions. Claiming a request atomically consumes its permit, increments
grant use, advances the session sequence, checks target fencing, and records a
content-free prepared effect. The journal never stores text, URLs, selectors,
file paths, screenshots, or raw target IDs supplied outside the opaque schema.

Execution states are:

```text
prepared -> started -> applied -> verified
       \-> failed      \-> unknown -> verified | failed
           started -> failed | unknown
```

The journal records `started` before adapter dispatch. Recovery may abandon a
`prepared` action as proven not dispatched. It never automatically re-dispatches
`started`, `applied`, or `unknown` work; it reconciles by effect ID. This makes a
crash before dispatch safely fail and a crash after a possible mutation
conservatively reconcile without duplicate uncertain mutation.

A mutating request cannot move to `verified` from an adapter's assertion
alone. Its reconciliation result must carry a different snapshot ID, a later
observation time and sequence, a non-regressing target generation and fence,
and an evidence digest over the exact postcondition. Same-generation results
must retain the exact target revision and fence; later-generation results must
advance the fence. Missing, stale, wrong-target, or digest-mismatched
postconditions remain `unknown` and are recorded as such. Observe and handoff
operations do not claim a mutation and therefore do not require this mutation
postcondition.

Verified, failed, and unknown outcomes receive an Ed25519-signed, content-free
receipt for their exact transition version. An unknown receipt may later be
followed by a distinct verified or failed reconciliation receipt; the earlier
receipt remains valid evidence of what was known at that time. Receipt schema 2
also signs a random journal identity, contiguous sequence, and previous-receipt
digest. Full-chain verification detects malformed rows, signature or digest
changes, broken links, interior deletion, and cross-journal substitution. A
caller can retain a signed head outside the journal and require it on a later
read to detect tail rollback. An anchored journal now automates that contract:
Grace stores the canonical signed head under a journal-specific label in a
recognized OS credential backend and uses compare-and-set under a cross-process
lease. Ada verifies the external signature and journal identity, proves that
the head is an exact prefix of the local chain, advances it only after the
SQLite receipt commits, and refuses missing, divergent, malformed, foreign, or
rolled-back anchors. A committed receipt whose anchor write is interrupted is
not returned as success; a later startup may catch up only from an already
authenticated prefix. Absence with an existing receipt chain fails closed and
requires operator recovery.

This remains a tamper-evident sequence, not filesystem immutability. The
`create_anchored_control_runtime` production posture requires the external
anchor; unanchored journals remain available only to the disabled simulators
and explicit tests. A local principal able to delete both the journal and its
credential item can still destroy evidence, and the current Keychain item is
owned by the Python process identity rather than a Developer ID native helper.
Production native composition, independent remote anchoring, package identity,
uninstall policy, and executed release provenance remain M7 work.

## Simulators and gates

Neon models browser targets/documents/elements, navigation, frame and element
staleness, redirects, denied redirects, quarantined popups, dialogs, service
worker restart, BFCache restore with a new generation, secure fields, upload
gating, hung/closed targets, coordinate invalidation, and idempotent effect
reconciliation. Austin models desktop processes/windows/AX elements, PID reuse,
relaunch, focus theft, app/system/auth/payment modals, secure fields, IME and
keyboard layout, hung/terminated targets, screen lock, inactive user sessions,
display/coordinate changes, file-picker handoff, and idempotent reconciliation.
Unavailable targets expose explicit handoff without recording a target mutation.
Neither simulator executes OS or browser code.

The David fuzzer uses deterministic malformed frames and bounded mutation. M4
does not close until 100,000 frames produce zero uncaught crashes/OOMs, every
stale/replayed/downgraded/over-limit/wrong-target/expired/revoked permit rejects,
and each injected transition crash demonstrates at most one adapter mutation.

## Verified M4 evidence

On 2026-07-19, 170 focused kernel, journal, runtime, fuzzer, Neon, and Austin
tests passed. The full repository regression passed 2,346 tests, repository-wide
Ruff, a 25-file mypy boundary, compileall, diff hygiene, the freeze gate, and a
strict dependency audit with no known vulnerabilities.

The authoritative fuzzer run used seed `2913838877` and rejected 100,000 of
100,000 cases across 25 evenly exercised modes. It observed zero unexpected
accepts, zero uncaught crashes, a maximum case size of 2,845 bytes, and a maximum
decoder buffer of 2,844 bytes. Its classification digest is
`sha256:15d9573259bfd3baf2e93b6bb0779e68610fbdc2c93ca064d756faa9f8f3b2ec`;
the frozen corpus digest is
`sha256:b6f6cfac73e23afe9c5436c9504b7621c79c55b117f79f2820e64fd77491d2b1`.
All eight declared crash checkpoints recovered with adapter dispatch at most
once and target mutation at most once.

Two adversarial findings were fixed before closure. The first 100,000-case run
found a noncanonical Base64URL spelling that decoded to the same signature
bytes; canonical re-encoding now rejects it. A reference Draft 2020-12 probe
then found 63 trailing-newline schema accepts caused by line-end semantics;
absolute end guards reduced that mismatch count to zero. Five exported schemas
and five canonical instances pass `jsonschema==4.25.1` validation.

This evidence proves the disabled, provider-neutral Python foundation and its
bounded simulators. It does not prove Chrome isolation, extension permissions,
network containment, real browser completion, a signed/notarized macOS broker,
TCC behavior, packaged-key durability, or public runtime readiness. Those remain
M5 through M9 gates.

## Primary references

- RFC 8259 JSON: <https://www.rfc-editor.org/rfc/rfc8259>
- JSON Schema 2020-12: <https://json-schema.org/draft/2020-12>
- RFC 8785 JSON Canonicalization Scheme: <https://www.rfc-editor.org/rfc/rfc8785>
- RFC 9562 UUIDs: <https://www.rfc-editor.org/rfc/rfc9562>
- RFC 8032 Ed25519: <https://www.rfc-editor.org/rfc/rfc8032>
- NIST replay resistance: <https://pages.nist.gov/800-63-4/sp800-63b.html>
- SQLite transactions and crash durability: <https://www.sqlite.org/transactional.html>
- SQLite atomic commit: <https://www.sqlite.org/atomiccommit.html>
- SQLite security pragmas: <https://www.sqlite.org/pragma.html>
