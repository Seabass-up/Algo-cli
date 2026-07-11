"""Stable capability bit masks (J11).

Apple's audit_class file uses stable numeric bit masks. This module gives
Algo CLI the same ABI-style capability vocabulary for tools/kernels while
preserving human-readable tier names.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Capability(IntEnum):
    READ = 1 << 0
    WRITE = 1 << 1
    SHELL = 1 << 2
    NETWORK = 1 << 3
    MODEL = 1 << 4
    CREDENTIAL = 1 << 5
    MEMORY = 1 << 6
    EXTERNAL_PUBLISH = 1 << 7
    DESTRUCTIVE = 1 << 8


TIER_MASKS: dict[str, int] = {
    "tier0": Capability.READ.value,
    "tier1": Capability.READ.value | Capability.NETWORK.value | Capability.MODEL.value,
    "tier2": Capability.READ.value | Capability.WRITE.value | Capability.SHELL.value | Capability.MODEL.value | Capability.MEMORY.value,
    "tier3": sum(cap.value for cap in Capability),
}


@dataclass(frozen=True)
class CapabilityMask:
    value: int = 0

    def has(self, capability: Capability) -> bool:
        return bool(self.value & capability.value)

    def add(self, capability: Capability) -> "CapabilityMask":
        return CapabilityMask(self.value | capability.value)

    def remove(self, capability: Capability) -> "CapabilityMask":
        return CapabilityMask(self.value & ~capability.value)

    def names(self) -> tuple[str, ...]:
        return tuple(cap.name.lower() for cap in Capability if self.has(cap))

    def to_dict(self) -> dict:
        return {"value": self.value, "capabilities": list(self.names())}


def tier_mask(tier: str) -> int:
    return TIER_MASKS.get((tier or "").strip().lower(), 0)


def mask_from_names(names: list[str] | tuple[str, ...]) -> CapabilityMask:
    value = 0
    for name in names or ():
        key = str(name).strip().upper()
        if key in Capability.__members__:
            value |= Capability[key].value
    return CapabilityMask(value)


__all__ = ["Capability", "CapabilityMask", "TIER_MASKS", "mask_from_names", "tier_mask"]
