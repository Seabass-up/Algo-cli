# CLI Memory Credentials

Echo-protected CLI auxiliary state does not require installation of the signed
native browser-control app. Before its first protected write, explicitly run:

```sh
algo-cli config memory status
algo-cli config memory provision
```

These commands are available before the normal startup preflight. Provisioning
uses the secure OS keyring and preserves the existing privacy key. It creates
only the fixed `algo-cli-runtime` / `elsie-memory-anchors-v1` credential, with an
authenticated, bounded map of content-free receipt heads. It does not create a
control-signing key, Ada credential registry, pairing secret, native census,
TCC permission, or browser authority. Local macOS Keychain is the qualified
operator recovery path; an unsupported backend or failed write blocks safely.

The optional `echo-veil` dependency must also be installed in the exact Python
environment used by the `algo-cli` launcher. Use the project's pinned
`algo-cli-runtime[echo-veil]` extra, not an arbitrary Echo checkout or replacement
profile. Credential readiness alone does not prove adapter or model readiness;
finish recovery with the authenticated Echo doctor and one bounded model turn.

Only the four Elsie auxiliary-store namespaces are accepted. The whole map is
authenticated with a domain-separated HMAC, limited to 64 heads and 60 KiB,
serialized under the existing cross-process keyring inventory lease, and read
back after every write. Each head remains bound to its journal, subject, and
next sequence. Memory payloads are not stored in this credential.

Existing dynamic receipt heads remain on their original native-registry path;
they are not copied, reset, or silently replaced. Conflicting routes, invalid
credentials, a missing key for existing state, and missing committed anchors
fail closed. Provisioning is idempotent, not a recovery-key reset. Do not delete
credentials to work around an integrity failure.

Native installation still requires the verified signed enumeration. Its
credential census and uninstall inventory now include the new fixed label.
Older signed registries remain valid, but all compiled fixed labels are included
in current inventory snapshots; an older installation receipt must be explicitly
recaptured by the installer before a changed credential inventory can be removed.

No external-browser qualification or production attestation is implied by this
CLI-only provisioning command.

## Local Verification - 2026-09-04

The installed Python 3.14 launcher was provisioned using the command above.
Existing credential fingerprints were unchanged; no native signing key or Ada
registry was created. Real startup and an authenticated one-shot model call with
required Echo protection succeeded, with zero tool calls and zero plaintext
fallback attempts. The selected saved model was preserved.

The final Algo suite passed 4261 tests with 32 platform skips and 68.03%
branch-aware coverage. The native Swift suite passed 89 tests. This is local
candidate evidence, not release or external-browser qualification. Full-history
publication checks still flag older commits with private markers.
