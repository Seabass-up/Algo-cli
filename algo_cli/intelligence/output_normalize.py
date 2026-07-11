"""H22 — Sequential Output Normalization Pipeline.

Toggleable pipeline stages for output normalization.
Mined from G0DM0D3 PAPER.md §3.5 STM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class PipelineStage:
    """A single normalization stage."""

    name: str
    enabled: bool = True
    transform: Callable[[str], str] = lambda x: x
    description: str = ""


class OutputNormalizationPipeline:
    """Sequential pipeline of toggleable normalization stages."""

    def __init__(self) -> None:
        self._stages: list[PipelineStage] = []

    def add_stage(
        self,
        name: str,
        transform: Callable[[str], str],
        enabled: bool = True,
        description: str = "",
    ) -> PipelineStage:
        stage = PipelineStage(
            name=name, enabled=enabled, transform=transform, description=description
        )
        self._stages.append(stage)
        return stage

    def enable(self, name: str) -> bool:
        stage = self._find(name)
        if stage is None:
            return False
        stage.enabled = True
        return True

    def disable(self, name: str) -> bool:
        stage = self._find(name)
        if stage is None:
            return False
        stage.enabled = False
        return True

    def _find(self, name: str) -> PipelineStage | None:
        for s in self._stages:
            if s.name == name:
                return s
        return None

    def run(self, text: str) -> str:
        """Run all enabled stages in sequence."""
        result = text
        for stage in self._stages:
            if stage.enabled:
                result = stage.transform(result)
        return result

    def get_stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def get_enabled_stages(self) -> list[str]:
        return [s.name for s in self._stages if s.enabled]

    def count(self) -> int:
        return len(self._stages)

    def remove_stage(self, name: str) -> bool:
        stage = self._find(name)
        if stage is None:
            return False
        self._stages.remove(stage)
        return True


# Built-in transforms
def hedge_reducer(text: str) -> str:
    """Reduce hedging language."""
    text = re.sub(r"\bI think\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmaybe\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bperhaps\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpossibly\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def direct_mode(text: str) -> str:
    """Convert indirect requests to direct."""
    text = re.sub(r"\bCould you\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bWould you\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCan you\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def casual_mode(text: str) -> str:
    """Make output more casual."""
    text = re.sub(r"\bTherefore\b", "So", text, flags=re.IGNORECASE)
    text = re.sub(r"\bHowever\b", "But", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()