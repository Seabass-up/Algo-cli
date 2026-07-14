# Task: Reconcile a medium-repository rollout

Three service configurations have drifted. Determine the authoritative release
source using the repository's source registry, then reconcile the gateway,
worker, and notifier settings to that live release while preserving unrelated
service settings.

Create `rollout_receipt.md` in the current run artifact directory. It must name
the live client, release ID, region, API base, feature flag, rollout window, and
explain that stale sources were rejected. Run `python verify_rollout.py` from the
run workspace and report the verified result.

Do not modify the source registry, release manifest, verifier, stale snapshot,
operator handoff, or files outside the run workspace and artifact directory.
