"""B76. Three Scales of Spawning.

Virtual File (2-4 agents) → Git Worktree (10-100) → Cloud Worker (100+).
Source: awesome-agentic-patterns.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class SpawnScale(Enum):
    VIRTUAL_FILE = auto()    # 2-4 agents, in-memory isolation
    GIT_WORKTREE = auto()    # 10-100 agents, filesystem isolation
    CLOUD_WORKER = auto()    # 100+ agents, container isolation


@dataclass
class ScaleConfig:
    scale: SpawnScale
    max_agents: int
    isolation_level: str  # "memory", "filesystem", "container"
    cost_per_agent: float  # relative cost
    setup_time_s: float   # time to spawn one agent
    cleanup_required: bool


DEFAULT_CONFIGS: dict[SpawnScale, ScaleConfig] = {
    SpawnScale.VIRTUAL_FILE: ScaleConfig(
        scale=SpawnScale.VIRTUAL_FILE,
        max_agents=4,
        isolation_level="memory",
        cost_per_agent=0.1,
        setup_time_s=0.01,
        cleanup_required=False,
    ),
    SpawnScale.GIT_WORKTREE: ScaleConfig(
        scale=SpawnScale.GIT_WORKTREE,
        max_agents=100,
        isolation_level="filesystem",
        cost_per_agent=0.5,
        setup_time_s=0.5,
        cleanup_required=True,
    ),
    SpawnScale.CLOUD_WORKER: ScaleConfig(
        scale=SpawnScale.CLOUD_WORKER,
        max_agents=1000,
        isolation_level="container",
        cost_per_agent=2.0,
        setup_time_s=5.0,
        cleanup_required=True,
    ),
}


class SpawnScaleSelector:
    """Select the appropriate spawning scale based on requirements."""

    def select(self, num_agents: int, needs_filesystem: bool = False,
               needs_network: bool = False, budget: float = 1.0) -> SpawnScale:
        """Choose the minimal scale that meets requirements."""
        if num_agents <= 4 and not needs_filesystem and not needs_network:
            return SpawnScale.VIRTUAL_FILE
        if num_agents <= 100 and not needs_network:
            return SpawnScale.GIT_WORKTREE
        return SpawnScale.CLOUD_WORKER

    def get_config(self, scale: SpawnScale) -> ScaleConfig:
        return DEFAULT_CONFIGS[scale]

    def estimate_cost(self, scale: SpawnScale, num_agents: int) -> float:
        config = self.get_config(scale)
        return config.cost_per_agent * num_agents

    def validate(self, scale: SpawnScale, num_agents: int) -> list[str]:
        """Check if the scale can handle the requested agent count."""
        issues: list[str] = []
        config = self.get_config(scale)
        if num_agents > config.max_agents:
            issues.append(f"Too many agents: {num_agents} > {config.max_agents} for {scale.name}")
        return issues

    def recommend(self, num_agents: int, needs_filesystem: bool = False,
                  needs_network: bool = False, budget: float = 1.0) -> dict[str, Any]:
        """Get a recommendation with cost estimate."""
        scale = self.select(num_agents, needs_filesystem, needs_network, budget)
        config = self.get_config(scale)
        cost = self.estimate_cost(scale, num_agents)
        issues = self.validate(scale, num_agents)

        return {
            "scale": scale.name,
            "isolation": config.isolation_level,
            "estimated_cost": cost,
            "setup_time_s": config.setup_time_s * num_agents,
            "cleanup_required": config.cleanup_required,
            "issues": issues,
        }