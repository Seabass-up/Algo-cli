"""B82. Golden Master: Characterization Tests.

Capture current behavior before refactoring.  Verify no regressions after.
Source: CCASP pattern.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class GoldenMaster:
    name: str
    inputs: list[Any] = field(default_factory=list)
    expected_outputs: list[Any] = field(default_factory=list)
    snapshots: dict[str, str] = field(default_factory=dict)  # input_hash → output_hash

    def capture(self, input_data: Any, output: Any) -> None:
        """Capture a golden master snapshot."""
        input_hash = self._hash(input_data)
        output_hash = self._hash(output)
        self.inputs.append(input_data)
        self.expected_outputs.append(output)
        self.snapshots[input_hash] = output_hash

    def verify(self, input_data: Any, output: Any) -> bool:
        """Verify output matches golden master."""
        input_hash = self._hash(input_data)
        output_hash = self._hash(output)
        return self.snapshots.get(input_hash) == output_hash

    def verify_all(self, run_fn: Callable[[Any], Any]) -> list[bool]:
        """Verify all captured inputs against current behavior."""
        results: list[bool] = []
        for input_data, expected in zip(self.inputs, self.expected_outputs):
            actual = run_fn(input_data)
            results.append(self.verify(input_data, actual))
        return results

    @staticmethod
    def _hash(data: Any) -> str:
        if isinstance(data, str):
            return hashlib.sha256(data.encode()).hexdigest()[:16]
        return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()[:16]

    def save(self, path: Path) -> None:
        """Save golden master to file."""
        path.write_text(json.dumps({
            "name": self.name,
            "snapshots": self.snapshots,
        }, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "GoldenMaster":
        """Load golden master from file."""
        data = json.loads(path.read_text(encoding="utf-8"))
        gm = cls(name=data["name"])
        gm.snapshots = data.get("snapshots", {})
        return gm


class GoldenMasterRunner:
    """Run golden master characterization tests."""

    def __init__(self) -> None:
        self._masters: dict[str, GoldenMaster] = {}

    def create(self, name: str) -> GoldenMaster:
        gm = GoldenMaster(name=name)
        self._masters[name] = gm
        return gm

    def get(self, name: str) -> GoldenMaster | None:
        return self._masters.get(name)

    def run_all(self, run_fn: Callable[[Any], Any]) -> dict[str, list[bool]]:
        """Run all golden masters against current behavior."""
        return {name: gm.verify_all(run_fn) for name, gm in self._masters.items()}

    def regression_report(self, run_fn: Callable[[Any], Any]) -> str:
        """Generate a report of any regressions."""
        lines: list[str] = ["Golden Master Regression Report", ""]
        all_pass = True
        for name, gm in self._masters.items():
            results = gm.verify_all(run_fn)
            passed = sum(results)
            total = len(results)
            status = "PASS" if passed == total else "FAIL"
            if passed != total:
                all_pass = False
            lines.append(f"  {name}: {passed}/{total} — {status}")
        lines.append("")
        lines.append("ALL PASS" if all_pass else "REGRESSIONS DETECTED")
        return "\n".join(lines)