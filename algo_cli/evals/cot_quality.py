"""CoT quality scoring (I1 + I3).

Scoring a CoT block on sequencing markers, length ratio, and verification
cadence. Used by algo-cli evals to flag under-thinking, over-thinking, and
stream-of-consciousness reasoning.

Provenance: ALGO.md I1 (CoT-Proportional Reasoning) and I3 (Sequenced
Reasoning Markers). Calibration: Fable-5 corpus (4,665 rows, 100% coverage
of cot/completion fields; median cot_ratio = 1.14, mean = 1.28).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class SequencePattern(str, Enum):
    """Recognized tool-sequence patterns from the Fable-5 trace audit (I7)."""

    EMPTY = "empty"
    TDD_EDIT_TEST_EDIT = "tdd_edit_test_edit"
    VERIFY_AFTER_EDIT = "verify_after_edit"
    SHELL_INSPECT_LOOP = "shell_inspect_loop"
    UNCLASSIFIED = "unclassified"


class Band(str, Enum):
    UNDER = "under_thinking"   # cot_ratio < 0.5
    IN_BAND = "in_band"        # 0.5 <= cot_ratio <= 5.0 (Fable-5 p90 = 1.79, max observed 4.2)
    OVER = "over_thinking"     # cot_ratio > 5.0


# Sequential markers found in well-structured CoT.
MARKER_RE = re.compile(
    r"\b(First|Next|Then|Finally|Step|Now)\b",
    re.IGNORECASE,
)
# "First, ... Next, ..." must appear in that order to count as well-sequenced.
WELL_SEQUENCED_RE = re.compile(
    r"\bFirst\b[\s\S]*?\bNext\b",
    re.IGNORECASE,
)


@dataclass
class ToolSequenceQuality:
    """Result of scoring a tool-call sequence for healthy verify cadence (I7)."""

    tool_names: tuple[str, ...]
    pattern: SequencePattern
    sequence_score: float
    verification_present: bool
    edit_count: int
    shell_count: int
    read_count: int
    summary: str

    def to_dict(self) -> dict:
        return {
            "tool_names": list(self.tool_names),
            "pattern": self.pattern.value,
            "sequence_score": round(self.sequence_score, 2),
            "verification_present": self.verification_present,
            "edit_count": self.edit_count,
            "shell_count": self.shell_count,
            "read_count": self.read_count,
            "summary": self.summary,
        }


@dataclass
class CoTQuality:
    """Result of scoring one CoT block against a completion string."""

    cot_chars: int
    completion_chars: int
    cot_ratio: float
    band: Band
    markers: tuple[str, ...]
    well_sequenced: bool
    structure_score: float
    summary: str

    def to_dict(self) -> dict:
        return {
            "cot_chars": self.cot_chars,
            "completion_chars": self.completion_chars,
            "cot_ratio": round(self.cot_ratio, 2),
            "band": self.band.value,
            "markers": list(self.markers),
            "well_sequenced": self.well_sequenced,
            "structure_score": round(self.structure_score, 2),
            "summary": self.summary,
        }


def _normalize_tool_name(name: str) -> str:
    lowered = (name or "").strip().lower()
    if "edit" in lowered or lowered in {"write_file", "batch_edit"}:
        return "edit"
    if "bash" in lowered or "shell" in lowered or lowered == "run_shell":
        return "bash"
    if "read" in lowered or lowered in {"grep", "search_files", "find_unique_anchor"}:
        return "read"
    return lowered or "unknown"


def score_tool_sequence(tool_names: list[str] | tuple[str, ...]) -> ToolSequenceQuality:
    """Score a sequence of tool calls for the Fable-5 TDD cadence (I7).

    The strongest healthy pattern is Edit→Bash→Edit: change, verify, repair.
    Bash→Bash→Read is recognized as an inspection loop, useful but weaker.
    """
    names = tuple(tool_names or ())
    normalized = [_normalize_tool_name(name) for name in names]
    edit_count = normalized.count("edit")
    shell_count = normalized.count("bash")
    read_count = normalized.count("read")
    verification_present = shell_count > 0

    pattern = SequencePattern.UNCLASSIFIED
    score = 0.0
    for idx in range(len(normalized) - 2):
        window = normalized[idx:idx + 3]
        if window == ["edit", "bash", "edit"]:
            pattern = SequencePattern.TDD_EDIT_TEST_EDIT
            score = 1.0
            break
    else:
        for idx in range(len(normalized) - 1):
            window = normalized[idx:idx + 2]
            if window == ["edit", "bash"]:
                pattern = SequencePattern.VERIFY_AFTER_EDIT
                score = 0.75
                break
        else:
            if len(normalized) >= 3 and normalized[:3] == ["bash", "bash", "read"]:
                pattern = SequencePattern.SHELL_INSPECT_LOOP
                score = 0.55
            elif not normalized:
                pattern = SequencePattern.EMPTY
                score = 0.0
            elif verification_present:
                score = 0.35
            elif read_count > 0:
                score = 0.2

    summary = (
        f"tools={len(names)}, pattern={pattern.value}, score={score:.2f}, "
        f"edit={edit_count}, shell={shell_count}, read={read_count}"
    )
    return ToolSequenceQuality(
        tool_names=names,
        pattern=pattern,
        sequence_score=score,
        verification_present=verification_present,
        edit_count=edit_count,
        shell_count=shell_count,
        read_count=read_count,
        summary=summary,
    )


def score_cot(cot: str, completion: str) -> CoTQuality:
    """Score one CoT block.

    Args:
        cot: The reasoning / thinking block preceding a tool call. May be empty.
        completion: The actual tool call or response. May be empty.

    Returns:
        A CoTQuality record with band, markers, well_sequenced, and a [0, 1]
        structure_score suitable for eval grading.
    """
    cot_len = len(cot or "")
    comp_len = len(completion or "")
    ratio = cot_len / max(1, comp_len)

    if ratio < 0.5:
        band = Band.UNDER
    elif ratio > 5.0:
        band = Band.OVER
    else:
        band = Band.IN_BAND

    markers = tuple(m.group(0) for m in MARKER_RE.finditer(cot or ""))
    well_seq = bool(WELL_SEQUENCED_RE.search(cot or ""))

    # marker_score capped at 0.6 (3 markers); seq_score 0.4; band_bonus 0.2
    marker_score = min(0.6, 0.2 * len(markers))
    seq_score = 0.4 if well_seq else 0.0
    band_bonus = 0.2 if band == Band.IN_BAND else 0.0
    score = min(1.0, marker_score + seq_score + band_bonus)

    summary = (
        f"cot={cot_len}ch, completion={comp_len}ch, ratio={ratio:.2f} "
        f"({band.value}), markers={len(markers)}, sequenced={well_seq}, "
        f"score={score:.2f}"
    )
    return CoTQuality(
        cot_chars=cot_len,
        completion_chars=comp_len,
        cot_ratio=ratio,
        band=band,
        markers=markers,
        well_sequenced=well_seq,
        structure_score=score,
        summary=summary,
    )


__all__ = [
    "Band",
    "CoTQuality",
    "SequencePattern",
    "ToolSequenceQuality",
    "score_cot",
    "score_tool_sequence",
]
