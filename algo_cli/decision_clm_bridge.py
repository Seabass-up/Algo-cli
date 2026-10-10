"""Use the installed CLM helper's pinned renderer/tokenizer before native inference."""

from __future__ import annotations

import json
import importlib
from pathlib import Path
import sys


def main() -> int:
    try:
        companion = Path(sys.argv[1]).resolve(strict=True)
        if companion.name != "clm_local.py":
            raise ValueError
        sys.path.insert(0, str(companion.parent))
        clm_local = importlib.import_module("clm_local")
        kernel_adapter = importlib.import_module("kernel_adapter")

        raw = sys.stdin.buffer.read(65_537)
        if len(raw) > 65_536:
            raise ValueError
        request = kernel_adapter.strict_json(raw)
        admission = kernel_adapter._token_budget(request)
        response = clm_local.call("/v1/systemone", {**request, "model": "clm-latest", "temperature": 1.0})
        kernel_adapter.validate_response(request, response)
        print(json.dumps({"ok": True, "response": response, "token_budget": admission}, allow_nan=False))
        return 0
    except Exception:
        # The companion manages its own logs; do not echo evidence or environment details.
        print(json.dumps({"ok": False, "error": "clm_admission_or_inference_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
