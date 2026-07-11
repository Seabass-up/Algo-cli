"""H17 — Negative Control Samples.

Known-clean samples to measure false positive rate.
Mined from T3MP3ST bench/cve-hunt/ DECOY samples.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ControlSample:
    """A single negative control sample."""

    id: str
    content: str
    label: str = "clean"
    expected_finding: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfusionMatrix:
    """Confusion matrix from running a detector against control samples."""

    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0

    @property
    def total(self) -> int:
        return self.true_positives + self.false_positives + self.true_negatives + self.false_negatives

    @property
    def false_positive_rate(self) -> float:
        tn_fp = self.true_negatives + self.false_positives
        return self.false_positives / tn_fp if tn_fp > 0 else 0.0

    @property
    def false_negative_rate(self) -> float:
        tp_fn = self.true_positives + self.false_negatives
        return self.false_negatives / tp_fn if tp_fn > 0 else 0.0

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.true_positives + self.true_negatives) / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "false_positive_rate": self.false_positive_rate,
            "false_negative_rate": self.false_negative_rate,
            "accuracy": self.accuracy,
        }


class NegativeControlSuite:
    """Manage negative control samples and compute confusion matrices."""

    def __init__(self) -> None:
        self._samples: list[ControlSample] = []

    def add_sample(
        self,
        id: str,
        content: str,
        expected_finding: bool = False,
        label: str = "clean",
        metadata: dict[str, Any] | None = None,
    ) -> ControlSample:
        sample = ControlSample(
            id=id,
            content=content,
            label=label,
            expected_finding=expected_finding,
            metadata=metadata or {},
        )
        self._samples.append(sample)
        return sample

    def evaluate(self, detector_results: dict[str, bool]) -> ConfusionMatrix:
        """Evaluate detector results against expected findings."""
        cm = ConfusionMatrix()
        for sample in self._samples:
            detected = detector_results.get(sample.id, False)
            if sample.expected_finding and detected:
                cm.true_positives += 1
            elif sample.expected_finding and not detected:
                cm.false_negatives += 1
            elif not sample.expected_finding and detected:
                cm.false_positives += 1
            else:
                cm.true_negatives += 1
        return cm

    def get_sample(self, sample_id: str) -> ControlSample | None:
        for s in self._samples:
            if s.id == sample_id:
                return s
        return None

    def all_samples(self) -> list[ControlSample]:
        return list(self._samples)

    def count(self) -> int:
        return len(self._samples)