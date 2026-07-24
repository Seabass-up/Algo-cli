#!/usr/bin/env python3
"""Deterministic bounded fuzzer for the disabled David control protocol."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
from typing import Any

# Henry launches this file as a child process.  Make that executable boundary
# independent of the caller's cwd, PYTHONPATH, or editable-install state.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.david_control_kernel import (  # noqa: E402
    CONTROL_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    ControlDataClass,
    ControlEnvelope,
    ControlKernelError,
    ControlRequest,
    ControlRoute,
    ControlSigner,
    FrameDecoder,
    Operation,
    SnapshotRef,
    TargetKind,
    TargetRef,
    default_control_policy,
    encode_frame,
    issue_grant,
    issue_permit,
    verify_envelope_authority,
)


FUZZ_SEED = 0xADA_DA71D
FUZZ_NOW_MS = 1_800_000_000_000
_MODES = (
    "bit_flip",
    "truncate",
    "append_junk",
    "zero_length",
    "oversized_length",
    "short_length",
    "long_length",
    "invalid_utf8",
    "bom",
    "duplicate_key",
    "float",
    "nonfinite",
    "unsafe_integer",
    "root_array",
    "excessive_depth",
    "downgrade",
    "extra_field",
    "missing_field",
    "wrong_message_type",
    "signature_tamper",
    "random_payload",
    "double_frame_junk",
    "stale_snapshot",
    "expired_permit",
    "no_live_route",
)


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DavidFuzzReport:
    iterations: int
    seed: int
    rejected: int
    unexpected_accepts: int
    unexpected_crashes: int
    maximum_case_bytes: int
    maximum_buffered_bytes: int
    corpus_digest: str
    classification_digest: str
    mode_counts: dict[str, int]

    @property
    def passed(self) -> bool:
        return (
            self.iterations > 0
            and self.rejected == self.iterations
            and self.unexpected_accepts == 0
            and self.unexpected_crashes == 0
            and set(self.mode_counts) == set(_MODES)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "iterations": self.iterations,
            "seed": self.seed,
            "rejected": self.rejected,
            "unexpected_accepts": self.unexpected_accepts,
            "unexpected_crashes": self.unexpected_crashes,
            "maximum_case_bytes": self.maximum_case_bytes,
            "maximum_buffered_bytes": self.maximum_buffered_bytes,
            "corpus_digest": self.corpus_digest,
            "classification_digest": self.classification_digest,
            "mode_counts": dict(sorted(self.mode_counts.items())),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class _FuzzFoundation:
    frame: bytes
    envelope_value: dict[str, Any]
    verifier: Any
    policy: Any
    snapshot: SnapshotRef


@dataclass(frozen=True, slots=True)
class _FuzzCase:
    mode: str
    wire_bytes: bytes
    now_ms: int
    live_routes: tuple[ControlRoute, ...]
    live_snapshot: SnapshotRef


def _foundation() -> _FuzzFoundation:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    policy = default_control_policy()
    target = TargetRef.from_dict(
        {
            "kind": TargetKind.BROWSER_DOCUMENT.value,
            "target_id": _opaque("fuzz-target"),
            "epoch": 7,
            "revision": "document-4",
            "fencing_token": 11,
        }
    )
    snapshot = SnapshotRef.from_dict(
        {
            "snapshot_id": _uuid(3),
            "target_id": target.target_id,
            "epoch": target.epoch,
            "revision": target.revision,
            "fencing_token": target.fencing_token,
            "observed_at_ms": FUZZ_NOW_MS - 10,
            "sequence": 1,
        }
    )
    request = ControlRequest.from_dict(
        {
            "schema_version": 1,
            "request_id": _uuid(1),
            "session_id": _uuid(2),
            "subject_id": "runtime.operator",
            "sequence": 1,
            "issued_at_ms": FUZZ_NOW_MS - 20,
            "deadline_ms": FUZZ_NOW_MS + 5_000,
            "target": target.to_dict(),
            "snapshot": snapshot.to_dict(),
            "operation": Operation.ACTIVATE.value,
            "data_class": ControlDataClass.STRUCTURAL.value,
            "arguments": {"element_id": _opaque("button")},
            "requested_routes": [ControlRoute.CONNECTOR.value],
            "max_output_bytes": 4096,
        }
    )
    grant = issue_grant(
        signer,
        policy,
        grant_id=_uuid(4),
        subject_id=request.subject_id,
        target_ids=(target.target_id,),
        target_kinds=(target.kind,),
        operations=(request.operation,),
        data_classes=(request.data_class,),
        routes=(ControlRoute.CONNECTOR,),
        issued_at_ms=FUZZ_NOW_MS - 1_000,
        expires_at_ms=FUZZ_NOW_MS + 10_000,
        maximum_action_count=1,
        max_input_bytes=policy.max_input_bytes,
        max_output_bytes=policy.max_output_bytes,
        max_transmit_bytes=0,
    )
    permit = issue_permit(
        signer,
        signer.verifier,
        policy,
        grant,
        request,
        permit_id=_uuid(5),
        issued_at_ms=FUZZ_NOW_MS - 5,
        expires_at_ms=FUZZ_NOW_MS + 1_000,
    )
    envelope = ControlEnvelope(request, grant, permit)
    return _FuzzFoundation(
        frame=envelope.to_frame(),
        envelope_value=envelope.to_dict(),
        verifier=signer.verifier,
        policy=policy,
        snapshot=snapshot,
    )


def _frame_payload(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _changed_snapshot(snapshot: SnapshotRef) -> SnapshotRef:
    value = snapshot.to_dict()
    value["fencing_token"] += 1
    return SnapshotRef.from_dict(value)


def _build_static_cases(foundation: _FuzzFoundation) -> dict[str, _FuzzCase]:
    value = foundation.envelope_value

    downgraded = deepcopy(value)
    downgraded["protocol_version"] = CONTROL_PROTOCOL_VERSION - 1
    extra = deepcopy(value)
    extra["model_instruction"] = "ignored"
    missing = deepcopy(value)
    missing.pop("permit")
    wrong_type = deepcopy(value)
    wrong_type["message_type"] = "control.generic"
    tampered = deepcopy(value)
    signature = tampered["permit"]["signature"]
    tampered["permit"]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]

    def case(
        mode: str,
        wire: bytes,
        *,
        now_ms: int = FUZZ_NOW_MS,
        routes: tuple[ControlRoute, ...] = (ControlRoute.CONNECTOR,),
        snapshot: SnapshotRef = foundation.snapshot,
    ) -> _FuzzCase:
        return _FuzzCase(mode, wire, now_ms, routes, snapshot)

    depth_value: Any = True
    for _ in range(20):
        depth_value = {"x": depth_value}
    depth_payload = json.dumps(depth_value, separators=(",", ":")).encode("ascii")
    return {
        "append_junk": case("append_junk", foundation.frame + b"x"),
        "zero_length": case("zero_length", struct.pack(">I", 0)),
        "oversized_length": case("oversized_length", struct.pack(">I", MAX_FRAME_BYTES + 1)),
        "invalid_utf8": case("invalid_utf8", _frame_payload(b'{"x":"\xff"}')),
        "bom": case("bom", _frame_payload(b"\xef\xbb\xbf{}")),
        "duplicate_key": case("duplicate_key", _frame_payload(b'{"x":1,"x":2}')),
        "float": case("float", _frame_payload(b'{"x":1.5}')),
        "nonfinite": case("nonfinite", _frame_payload(b'{"x":NaN}')),
        "unsafe_integer": case("unsafe_integer", _frame_payload(b'{"x":9007199254740992}')),
        "root_array": case("root_array", _frame_payload(b"[]")),
        "excessive_depth": case("excessive_depth", _frame_payload(depth_payload)),
        "downgrade": case("downgrade", encode_frame(downgraded)),
        "extra_field": case("extra_field", encode_frame(extra)),
        "missing_field": case("missing_field", encode_frame(missing)),
        "wrong_message_type": case("wrong_message_type", encode_frame(wrong_type)),
        "signature_tamper": case("signature_tamper", encode_frame(tampered)),
        "double_frame_junk": case("double_frame_junk", foundation.frame + _frame_payload(b"[]")),
        "stale_snapshot": case(
            "stale_snapshot",
            foundation.frame,
            snapshot=_changed_snapshot(foundation.snapshot),
        ),
        "expired_permit": case(
            "expired_permit",
            foundation.frame,
            now_ms=FUZZ_NOW_MS + 1_000,
        ),
        "no_live_route": case(
            "no_live_route",
            foundation.frame,
            routes=(ControlRoute.AX,),
        ),
    }


def _dynamic_case(
    foundation: _FuzzFoundation,
    static: dict[str, _FuzzCase],
    mode: str,
    rng: random.Random,
) -> _FuzzCase:
    frame = foundation.frame
    if mode == "bit_flip":
        changed = bytearray(frame)
        index = rng.randrange(4, len(changed))
        changed[index] ^= 1 << rng.randrange(8)
        return _FuzzCase(
            mode,
            bytes(changed),
            FUZZ_NOW_MS,
            (ControlRoute.CONNECTOR,),
            foundation.snapshot,
        )
    if mode == "truncate":
        end = rng.randrange(0, len(frame))
        return _FuzzCase(
            mode,
            frame[:end],
            FUZZ_NOW_MS,
            (ControlRoute.CONNECTOR,),
            foundation.snapshot,
        )
    if mode == "short_length":
        declared = max(1, len(frame) - 4 - rng.randrange(1, min(64, len(frame) - 4)))
        return _FuzzCase(
            mode,
            struct.pack(">I", declared) + frame[4:],
            FUZZ_NOW_MS,
            (ControlRoute.CONNECTOR,),
            foundation.snapshot,
        )
    if mode == "long_length":
        declared = len(frame) - 4 + rng.randrange(1, 64)
        return _FuzzCase(
            mode,
            struct.pack(">I", declared) + frame[4:],
            FUZZ_NOW_MS,
            (ControlRoute.CONNECTOR,),
            foundation.snapshot,
        )
    if mode == "random_payload":
        length = rng.randrange(1, 257)
        payload = rng.randbytes(length)
        return _FuzzCase(
            mode,
            _frame_payload(payload),
            FUZZ_NOW_MS,
            (ControlRoute.CONNECTOR,),
            foundation.snapshot,
        )
    return static[mode]


def _reject_case(
    foundation: _FuzzFoundation,
    case: _FuzzCase,
    rng: random.Random,
) -> tuple[str, int]:
    decoder = FrameDecoder()
    maximum_buffered = 0
    decoded: list[dict[str, Any]] = []
    try:
        cursor = 0
        while cursor < len(case.wire_bytes):
            chunk_size = rng.randrange(1, min(257, len(case.wire_bytes) - cursor) + 1)
            chunk = case.wire_bytes[cursor : cursor + chunk_size]
            cursor += len(chunk)
            decoded.extend(decoder.feed(chunk))
            maximum_buffered = max(maximum_buffered, decoder.buffered_bytes)
        decoder.finish()
        if len(decoded) != 1:
            raise ControlKernelError("frame_count")
        envelope = ControlEnvelope.from_dict(decoded[0])
        verify_envelope_authority(
            envelope,
            foundation.verifier,
            foundation.policy,
            now_ms=case.now_ms,
            live_routes=case.live_routes,
            live_snapshot=case.live_snapshot,
        )
    except ControlKernelError:
        return ("rejected", maximum_buffered)
    return ("accepted", maximum_buffered)


def fuzz_control_frames(
    *,
    iterations: int = 100_000,
    seed: int = FUZZ_SEED,
) -> DavidFuzzReport:
    if type(iterations) is not int or not len(_MODES) <= iterations <= 1_000_000:
        raise ValueError("iterations")
    if type(seed) is not int or not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("seed")
    foundation = _foundation()
    static = _build_static_cases(foundation)

    # A valid fragmented control proves the parser is operational before fuzzing.
    control_decoder = FrameDecoder()
    control_values: list[dict[str, Any]] = []
    for byte in foundation.frame:
        control_values.extend(control_decoder.feed(bytes((byte,))))
    control_decoder.finish()
    if control_values != [foundation.envelope_value]:
        raise RuntimeError("valid_control_failed")

    rng = random.Random(seed)
    rejected = 0
    unexpected_accepts = 0
    unexpected_crashes = 0
    maximum_case_bytes = 0
    maximum_buffered_bytes = 0
    mode_counts: Counter[str] = Counter()
    classifier = hashlib.sha256()
    for index in range(iterations):
        mode = _MODES[index % len(_MODES)]
        case = _dynamic_case(foundation, static, mode, rng)
        mode_counts[mode] += 1
        maximum_case_bytes = max(maximum_case_bytes, len(case.wire_bytes))
        try:
            classification, buffered = _reject_case(foundation, case, rng)
        except Exception:
            classification = "crashed"
            buffered = 0
            unexpected_crashes += 1
        else:
            if classification == "rejected":
                rejected += 1
            else:
                unexpected_accepts += 1
        maximum_buffered_bytes = max(maximum_buffered_bytes, buffered)
        case_digest = hashlib.sha256(case.wire_bytes).hexdigest()
        route_binding = ",".join(route.value for route in case.live_routes)
        classifier.update(
            (
                f"{index}:{mode}:{classification}:{case_digest}:"
                f"{case.now_ms}:{route_binding}:{case.live_snapshot.fencing_token}\n"
            ).encode("ascii")
        )

    # A second valid control catches accidental global parser poisoning.
    final_decoder = FrameDecoder()
    if final_decoder.feed(foundation.frame) != [foundation.envelope_value]:
        raise RuntimeError("valid_control_poisoned")
    final_decoder.finish()
    return DavidFuzzReport(
        iterations=iterations,
        seed=seed,
        rejected=rejected,
        unexpected_accepts=unexpected_accepts,
        unexpected_crashes=unexpected_crashes,
        maximum_case_bytes=maximum_case_bytes,
        maximum_buffered_bytes=maximum_buffered_bytes,
        corpus_digest="sha256:" + hashlib.sha256(foundation.frame).hexdigest(),
        classification_digest="sha256:" + classifier.hexdigest(),
        mode_counts=dict(mode_counts),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=FUZZ_SEED)
    arguments = parser.parse_args(argv)
    report = fuzz_control_frames(
        iterations=arguments.iterations,
        seed=arguments.seed,
    )
    print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
