"""B54. Boundary-Aware Context Compaction.

Preserve function-call pairs atomically during compaction.
Source: openai-agents-context-compaction pattern.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Message:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None
    token_estimate: int = 0


@dataclass
class CompactionResult:
    messages: list[Message]
    removed_count: int
    preserved_pairs: int
    summary: str = ""


class BoundaryAwareCompactor:
    """Compact context while preserving function-call pairs atomically."""

    def __init__(self, target_tokens: int = 8000, min_keep: int = 4):
        self.target_tokens = target_tokens
        self.min_keep = min_keep

    def _estimate_tokens(self, msg: Message) -> int:
        if msg.token_estimate:
            return msg.token_estimate
        return max(1, len(msg.content) // 4)

    def _total_tokens(self, messages: list[Message]) -> int:
        return sum(self._estimate_tokens(m) for m in messages)

    def _find_call_pairs(self, messages: list[Message]) -> list[tuple[int, int]]:
        """Find (assistant_with_tool_call, tool_response) index pairs."""
        pairs: list[tuple[int, int]] = []
        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                for call in msg.tool_calls:
                    call_id = call.get("id")
                    for j in range(i + 1, len(messages)):
                        if messages[j].tool_call_id == call_id:
                            pairs.append((i, j))
                            break
        return pairs

    def compact(self, messages: list[Message]) -> CompactionResult:
        """Compact messages to fit within target_tokens."""
        total = self._total_tokens(messages)
        if total <= self.target_tokens:
            return CompactionResult(messages=messages, removed_count=0, preserved_pairs=0)

        pairs = self._find_call_pairs(messages)
        pair_indices: set[int] = set()
        for a, b in pairs:
            pair_indices.add(a)
            pair_indices.add(b)

        # Always keep the last min_keep messages
        protected = set(range(max(0, len(messages) - self.min_keep), len(messages)))
        protected |= pair_indices

        # Remove oldest non-protected messages until under target
        removed = 0
        result = list(messages)
        i = 0
        while self._total_tokens(result) > self.target_tokens and i < len(result):
            if i in protected:
                i += 1
                continue
            result.pop(i)
            removed += 1
            # Rebuild protected set indices after removal
            protected = set(range(max(0, len(result) - self.min_keep), len(result)))
            # Rebuild pair indices
            pair_indices.clear()
            new_pairs = self._find_call_pairs(result)
            for a, b in new_pairs:
                pair_indices.add(a)
                pair_indices.add(b)
            protected |= pair_indices
        else:
            i += 1

        return CompactionResult(
            messages=result,
            removed_count=removed,
            preserved_pairs=len(pairs),
        )