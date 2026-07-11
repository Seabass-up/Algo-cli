"""Reflexion+ Verbal Self-Critique with Episodic Memory.

Extends the base reflex module with:
- Verbal self-evaluation after each attempt
- Episodic memory of past critiques for cross-attempt learning
- Retry with critique-guided modifications
- Convergence detection (when critiques stop surfacing new issues)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..chat_protocol import get_attr


@dataclass
class ReflexionEpisode:
    """One attempt in a Reflexion loop."""
    attempt: int
    task: str
    output: str
    critique: str
    score: float  # 0.0-1.0 self-assessment
    improved: bool  # did this attempt improve over the previous?
    timestamp: float = field(default_factory=time.time)

    def critique_message(self) -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                f"## Self-Critique for Attempt {self.attempt}\n"
                f"Score: {self.score:.2f}/1.0\n"
                f"{self.critique}\n\n"
                "Revise your approach to address these issues. Do not repeat the same mistakes."
            ),
        }


CRITIQUE_PROMPT = """You are evaluating your own work. Be specific and constructive.

Rate your output on a scale of 0.0 to 1.0:
- 1.0: Fully correct, complete, no issues
- 0.7: Mostly correct with minor gaps
- 0.4: Partially correct with significant issues
- 0.0: Fundamentally wrong or missing

For each issue found:
1. State the specific problem
2. Explain why it is a problem
3. Suggest a concrete fix

Format your response as JSON:
{"score": <float>, "critique": "<specific issues and fixes>"}
"""

CONVERGENCE_THRESHOLD = 0.05  # Score improvement below this = converged


@dataclass
class ReflexionLoop:
    """Stateful Reflexion loop for agent harness integration."""
    max_attempts: int = 3
    convergence_threshold: float = CONVERGENCE_THRESHOLD

    episodes: list[ReflexionEpisode] = field(default_factory=list)
    best_output: str = ""
    best_score: float = 0.0

    def is_converged(self) -> bool:
        """Check if recent episodes show diminishing returns."""
        if len(self.episodes) < 2:
            return False
        recent = self.episodes[-2:]
        improvement = abs(recent[1].score - recent[0].score)
        return improvement < self.convergence_threshold and recent[1].score >= recent[0].score

    def add_episode(self, episode: ReflexionEpisode) -> None:
        """Record an episode and update the best-so-far result.

        ``improved`` describes progress over the immediately preceding attempt,
        not whether the episode established a new all-time best.  Keeping those
        concepts separate matters after a regression followed by a partial
        recovery (for example, scores of 0.8, 0.5, then 0.7).
        """
        previous = self.episodes[-1] if self.episodes else None
        episode.improved = previous is not None and episode.score > previous.score
        self.episodes.append(episode)
        if len(self.episodes) == 1 or episode.score > self.best_score:
            self.best_score = episode.score
            self.best_output = episode.output

    def build_memory_context(self) -> str:
        """Build the episodic memory injection for the next attempt."""
        if not self.episodes:
            return ""
        lines = ["## Previous Attempts and Self-Critiques"]
        for ep in self.episodes:
            lines.append(f"### Attempt {ep.attempt} (score: {ep.score:.2f})")
            lines.append(f"Critique: {ep.critique[:500]}")
        lines.append("\nLearn from these critiques. Avoid repeating the same errors.")
        return "\n".join(lines)

    def build_messages(self, task: str, system: str) -> list[dict[str, Any]]:
        """Build messages for the next attempt, including episodic memory."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
        ]
        memory = self.build_memory_context()
        if memory:
            messages.append({"role": "user", "content": memory})
        messages.append({"role": "user", "content": task})
        return messages


def _parse_critique(response_text: str) -> tuple[float, str]:
    """Parse a critique response into (score, critique_text)."""
    text = response_text.strip()
    # Try JSON parse
    try:
        data = json.loads(text)
        score = float(data.get("score", 0.5))
        critique = str(data.get("critique", ""))
        return max(0.0, min(1.0, score)), critique
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # Try extracting score from text
    score_m = None
    for pattern in [r'"score"\s*:\s*([0-9.]+)', r'score:\s*([0-9.]+)', r'([0-9.]+)\s*/\s*1\.0']:
        import re
        m = re.search(pattern, text)
        if m:
            score_m = m
            break
    score = float(score_m.group(1)) if score_m else 0.5
    return max(0.0, min(1.0, score)), text[:1000]


def run_reflexion_loop(
    *,
    task: str,
    client: Any,
    model: str,
    critique_model: str | None = None,
    system: str = "You are a capable reasoning agent. Produce your best work on the given task.",
    max_attempts: int = 3,
    tools: list[Any] | None = None,
    score_threshold: float = 0.8,
) -> list[ReflexionEpisode]:
    """Run a Reflexion+ loop: attempt -> self-critique -> retry.

    Args:
        task: The task to solve.
        client: Ollama client instance.
        model: Model for task attempts.
        critique_model: Optional separate model for self-critique (defaults to model).
        system: System prompt.
        max_attempts: Maximum reflexion attempts.
        tools: Optional tools for the model.
        score_threshold: Stop early if score exceeds this.

    Returns:
        List of ReflexionEpisode records.
    """
    critique_model = critique_model or model
    loop = ReflexionLoop(max_attempts=max_attempts)

    for attempt in range(1, max_attempts + 1):
        # 1. Attempt the task
        messages = loop.build_messages(task, system)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            response = client.chat(**kwargs)
            output = get_attr(get_attr(response, "message", {}), "content", "")
        except Exception as exc:
            output = f"Error during attempt {attempt}: {exc}"

        # 2. Self-critique
        critique_messages = [
            {"role": "system", "content": CRITIQUE_PROMPT},
            {"role": "user", "content": f"## Task\n{task}\n\n## Your Output\n{output}\n\nEvaluate your output above."},
        ]
        try:
            critique_response = client.chat(
                model=critique_model,
                messages=critique_messages,
                stream=False,
                format="json",
            )
            critique_text = get_attr(get_attr(critique_response, "message", {}), "content", "")
            score, critique = _parse_critique(critique_text)
        except Exception:
            score = 0.5
            critique = "(critique generation failed)"

        episode = ReflexionEpisode(
            attempt=attempt,
            task=task,
            output=output,
            critique=critique,
            score=score,
            # add_episode derives the value from the previous episode.  Supply
            # the neutral value here so construction cannot fail before that
            # comparison is made.
            improved=False,
        )
        loop.add_episode(episode)

        # 3. Check termination
        if score >= score_threshold:
            break
        if loop.is_converged() and attempt >= 2:
            break

    return loop.episodes
