"""B160, B172: Acrobat-derived model and hardware compatibility patterns.

- B160: Local Model Manifest Registry + Tensor Schema Gate
- B172: Hardware/Platform Compatibility Table
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── B160: Local Model Manifest Registry + Tensor Schema Gate ──────────


@dataclass
class TensorSpec:
    """A tensor input/output specification."""
    name: str
    shape: list[int]
    data_type: str  # "float", "int64", etc.
    required: bool = True


@dataclass
class ModelComponent:
    """A component file for a model."""
    name: str
    path: str
    encryption_key: str = "none"


@dataclass
class ModelTarget:
    """A runtime target for a model."""
    runtime: str  # "WinML", "onnxruntime", "ollama", etc.
    components: list[ModelComponent] = field(default_factory=list)
    inputs: list[TensorSpec] = field(default_factory=list)
    outputs: list[TensorSpec] = field(default_factory=list)


@dataclass
class ModelManifest:
    """A local model manifest (B160)."""
    id: str
    name: str
    author: str = ""
    version: int = 1
    targets: dict[str, ModelTarget] = field(default_factory=dict)

    def get_target(self, runtime: str) -> ModelTarget | None:
        return self.targets.get(runtime)


class ModelSchemaGate:
    """Validates tensor schemas before model invocation (B160)."""

    @staticmethod
    def validate_input(target: ModelTarget, provided_inputs: dict[str, Any]) -> list[str]:
        """Validate provided inputs against the target's input specs.
        Returns list of errors (empty = valid).
        """
        errors: list[str] = []
        for spec in target.inputs:
            if spec.required and spec.name not in provided_inputs:
                errors.append(f"missing required input: {spec.name}")
                continue
            if spec.name in provided_inputs:
                value = provided_inputs[spec.name]
                # Check shape if value has shape attribute
                if hasattr(value, "shape"):
                    actual_shape = list(value.shape)
                    if len(actual_shape) != len(spec.shape):
                        errors.append(
                            f"shape mismatch for {spec.name}: expected {spec.shape}, got {actual_shape}"
                        )
        return errors

    @staticmethod
    def validate_runtime(target: ModelTarget, available_runtimes: set[str]) -> bool:
        """Check if the target's runtime is available."""
        return target.runtime in available_runtimes


class ModelManifestRegistry:
    """Registry of local model manifests (B160)."""

    def __init__(self) -> None:
        self._manifests: dict[str, ModelManifest] = {}

    def register(self, manifest: ModelManifest) -> None:
        self._manifests[manifest.id] = manifest

    def get(self, model_id: str) -> ModelManifest | None:
        return self._manifests.get(model_id)

    def load_check(
        self, model_id: str, runtime: str, existing_files: set[str], available_runtimes: set[str] | None = None
    ) -> tuple[bool, list[str]]:
        """Check if a model can be loaded. Returns (can_load, errors)."""
        manifest = self._manifests.get(model_id)
        if not manifest:
            return False, [f"unknown model: {model_id}"]
        target = manifest.get_target(runtime)
        if not target:
            return False, [f"runtime not supported: {runtime}"]
        if available_runtimes is not None and not ModelSchemaGate.validate_runtime(target, available_runtimes):
            return False, [f"runtime not available: {runtime}"]
        errors: list[str] = []
        for comp in target.components:
            if comp.path not in existing_files:
                errors.append(f"missing component file: {comp.path}")
        if errors:
            return False, errors
        return True, []

    def all_models(self) -> list[ModelManifest]:
        return list(self._manifests.values())


# ── B172: Hardware/Platform Compatibility Table ────────────────────────


@dataclass
class CompatEntry:
    """A single hardware compatibility entry (B172)."""
    action: str  # "opt_in", "opt_out", "restrict"
    vendor_id: str = ""
    driver: str = ""
    device_name: str = ""
    flags: str = ""
    feature: str = "gpu"  # which feature this entry controls


@dataclass
class CompatDecision:
    """Result of a compatibility check."""
    enabled: bool
    reason: str = ""
    entry_id: str = ""


class HardwareCompatTable:
    """Hardware/platform compatibility table (B172)."""

    def __init__(self) -> None:
        self._entries: list[CompatEntry] = []

    def add_entry(self, entry: CompatEntry) -> None:
        self._entries.append(entry)

    def check(self, vendor_id: str, driver: str = "", device_name: str = "", feature: str = "gpu") -> CompatDecision:
        """Check if a feature should be enabled for the given hardware."""
        # Look for exact match first
        for i, entry in enumerate(self._entries):
            if entry.feature != feature:
                continue
            if entry.vendor_id and entry.vendor_id != vendor_id:
                continue
            if entry.driver and driver and entry.driver != driver:
                continue
            if entry.device_name and entry.device_name != device_name:
                continue
            if entry.action == "opt_in":
                return CompatDecision(enabled=True, reason=f"opt_in by entry {i}", entry_id=str(i))
            if entry.action == "opt_out":
                return CompatDecision(enabled=False, reason=f"opt_out by entry {i}", entry_id=str(i))
            if entry.action == "restrict":
                return CompatDecision(enabled=False, reason=f"restricted by entry {i}", entry_id=str(i))
        # Default: conservative opt-out for unknown hardware
        return CompatDecision(enabled=False, reason="unknown hardware, conservative opt-out")

    def all_entries(self) -> list[CompatEntry]:
        return list(self._entries)


def default_gpu_compat_table() -> HardwareCompatTable:
    """Create a default GPU compatibility table from Acrobat's AGMGPUOptIn.ini."""
    table = HardwareCompatTable()
    # Intel G965 Express - opt out
    table.add_entry(CompatEntry(
        action="opt_out", vendor_id="00008086", driver="igxprd32.dll",
        device_name="G965 EXPRESS", feature="gpu",
    ))
    # AMD Radeon - opt in
    table.add_entry(CompatEntry(
        action="opt_in", vendor_id="00001002", driver="atiumdag",
        device_name="RADEON", feature="gpu",
    ))
    # NVIDIA GeForce - opt in
    table.add_entry(CompatEntry(
        action="opt_in", vendor_id="000010de", driver="nvd3dum",
        device_name="GEFORCE", feature="gpu",
    ))
    return table