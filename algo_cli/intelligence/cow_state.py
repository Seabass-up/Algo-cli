"""B71. Copy-On-Write State Isolation for Parallel Agents.

COW state per parallel agent.  Shared read-only history.
Diff merge back to main context.
Source: gecko pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class COWState:
    """Copy-on-write state for a parallel agent."""
    agent_id: str
    parent_state: dict[str, Any]  # read-only reference
    local_changes: dict[str, Any] = field(default_factory=dict)
    deleted_keys: set[str] = field(default_factory=set)

    def get(self, key: str) -> Any:
        if key in self.deleted_keys:
            return None
        if key in self.local_changes:
            return self.local_changes[key]
        return self.parent_state.get(key)

    def set(self, key: str, value: Any) -> None:
        self.local_changes[key] = value
        self.deleted_keys.discard(key)

    def delete(self, key: str) -> None:
        self.deleted_keys.add(key)
        self.local_changes.pop(key, None)

    def diff(self) -> dict[str, Any]:
        """Return only the changes."""
        return {
            "added_or_modified": dict(self.local_changes),
            "deleted": list(self.deleted_keys),
        }

    def snapshot(self) -> dict[str, Any]:
        """Full snapshot: parent + local changes."""
        result = dict(self.parent_state)
        result.update(self.local_changes)
        for key in self.deleted_keys:
            result.pop(key, None)
        return result


class COWStateManager:
    """Manage COW states for parallel agents."""

    def __init__(self) -> None:
        self._main_state: dict[str, Any] = {}
        self._agent_states: dict[str, COWState] = {}

    @property
    def main_state(self) -> dict[str, Any]:
        return dict(self._main_state)

    def update_main(self, key: str, value: Any) -> None:
        self._main_state[key] = value

    def fork(self, agent_id: str) -> COWState:
        """Create a COW fork for an agent."""
        state = COWState(agent_id=agent_id, parent_state=self._main_state)
        self._agent_states[agent_id] = state
        return state

    def get_state(self, agent_id: str) -> COWState | None:
        return self._agent_states.get(agent_id)

    def merge(self, agent_id: str) -> dict[str, Any]:
        """Merge agent's changes back to main state."""
        state = self._agent_states.get(agent_id)
        if not state:
            return dict(self._main_state)

        # Apply changes
        for key, value in state.local_changes.items():
            self._main_state[key] = value
        for key in state.deleted_keys:
            self._main_state.pop(key, None)

        # Clean up agent state
        del self._agent_states[agent_id]
        return dict(self._main_state)

    def merge_all(self) -> dict[str, Any]:
        """Merge all agent states back to main."""
        for agent_id in list(self._agent_states.keys()):
            self.merge(agent_id)
        return dict(self._main_state)

    def discard(self, agent_id: str) -> None:
        """Discard an agent's changes without merging."""
        self._agent_states.pop(agent_id, None)

    @property
    def active_forks(self) -> int:
        return len(self._agent_states)