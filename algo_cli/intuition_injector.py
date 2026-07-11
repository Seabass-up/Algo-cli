from typing import Any
import logging
import time

logger = logging.getLogger("intuition_injector")

_recent_recalls: dict[str, float] = {}
_CACHE_TTL = 300


def _format_recall_block(block: dict) -> str:
    """Produce a clean, evidence-backed recall block."""
    block_id = block.get("id", "unknown")
    score = block.get("score", 0)
    content = block.get("content", "").strip()
    block_type = block.get("type", "general")
    timestamp = block.get("timestamp", "")

    header = f"**[{block_type.upper()}]** `{block_id}` - Score: {score:.2f}"
    if timestamp:
        header += f" - {timestamp[:10]}"

    # Keep context small and focused.
    body = content[:450].replace("\n", " ").strip()
    if len(content) > 450:
        body += "..."

    return f"{header}\n{body}"


def inject_intuition(
    system_prompt: str,
    user_input: str,
    intuition_engine: Any,
    top_k: int = 3,
    min_score: float = 0.65,
    max_tokens: int = 1200,
) -> str:
    """Inject relevant intuition recalls with structured, polished format."""
    if not intuition_engine:
        return system_prompt

    try:
        recalled = intuition_engine.recall(user_input, top_k=top_k, min_score=min_score)
        if not recalled:
            return system_prompt

        # Filter recent recalls.
        now = time.time()
        filtered = []
        for block in recalled:
            block_id = block.get("id", "")
            if block_id and _recent_recalls.get(block_id, 0) > now - _CACHE_TTL:
                continue
            filtered.append(block)
            if block_id:
                _recent_recalls[block_id] = now

        if not filtered:
            return system_prompt

        # Build structured output within token budget.
        blocks_text = ""
        current_tokens = 0
        for block in filtered:
            block_text = _format_recall_block(block) + "\n\n"
            block_tokens = len(block_text) // 4

            if current_tokens + block_tokens > max_tokens:
                break

            blocks_text += block_text
            current_tokens += block_tokens

        if not blocks_text.strip():
            return system_prompt

        return f"{system_prompt}\n\n### Relevant Intuition\n{blocks_text.strip()}"

    except Exception as exc:
        logger.warning("Intuition recall failed: %s", exc)
        return system_prompt
