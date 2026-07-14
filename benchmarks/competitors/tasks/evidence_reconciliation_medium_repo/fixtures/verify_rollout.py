from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "control_plane/release_manifest.json").read_text())
EXPECTED = {
    "services/gateway/settings.json": {
        "apiEndpoint": MANIFEST["api_base"],
        "deploymentRegion": MANIFEST["region"],
        "releaseId": MANIFEST["release_id"],
        "featureFlag": MANIFEST["feature_flag"],
        "timeoutSeconds": 30,
    },
    "services/worker/settings.json": {
        "upstreamUrl": MANIFEST["api_base"],
        "region": MANIFEST["region"],
        "rollout": MANIFEST["release_id"],
        "featureFlag": MANIFEST["feature_flag"],
        "maxJobs": 8,
    },
    "services/notifier/settings.json": {
        "baseUrl": MANIFEST["api_base"],
        "zone": MANIFEST["region"],
        "release": MANIFEST["release_id"],
        "featureFlag": MANIFEST["feature_flag"],
        "channel": "ops",
    },
}


for relative, expected in EXPECTED.items():
    actual = json.loads((ROOT / relative).read_text())
    if actual != expected:
        raise SystemExit(f"FAIL {relative}: configuration does not match live manifest")

print("PASS rollout configuration matches the authoritative live manifest")
