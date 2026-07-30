#!/usr/bin/env python3
"""Build and notarize the Austin Developer ID installer without raw secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from algo_cli import __version__
from algo_cli.austin_release_packager import (
    AustinReleaseConfig,
    AustinReleasePackager,
    AustinReleaseRejected,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-identity", required=True)
    parser.add_argument("--installer-identity", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--notary-profile", required=True)
    parser.add_argument("--extension-origin", required=True)
    parser.add_argument("--disabled-native-authority-public-key", required=True, type=Path)
    parser.add_argument("--disabled-native-authority-public-key-digest", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--build-number", required=True)
    args = parser.parse_args(argv)
    config = AustinReleaseConfig(
        application_identity=args.application_identity,
        installer_identity=args.installer_identity,
        team_id=args.team_id,
        notary_profile=args.notary_profile,
        extension_origin=args.extension_origin,
        disabled_native_authority_public_key=(args.disabled_native_authority_public_key.expanduser().absolute()),
        disabled_native_authority_public_key_digest=(args.disabled_native_authority_public_key_digest),
        output_directory=args.output_directory.expanduser().absolute(),
        version=args.version,
        build_number=args.build_number,
    )
    try:
        result = AustinReleasePackager().build(config)
    except AustinReleaseRejected as exc:
        print(
            json.dumps(
                {"reason_code": exc.reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {
                "evidence": result.evidence_path.name,
                "package": result.package_path.name,
                "package_digest": result.package_digest,
                "status": "passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
