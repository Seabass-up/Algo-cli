"""QCR-LLM Quantum-Inspired Combinatorial Reasoning.

Reformulates multi-sample Chain-of-Thought aggregation as a Higher-Order
Unconstrained Binary Optimization (HUBO) problem, then solves it using
classical simulated annealing (default) with optional QPU offload.

Energy function:
    H(x) = sum_{S subset [R], |S|<=K} w_S * prod_{i in S} x_i

Weights w_S encode:
- Popularity: how many samples agree on fragment i
- Correlations: pairwise cosine similarity between fragment embeddings
- Higher-order coherence: 3-body+ semantic consistency

Local-first: uses simulated annealing by default.
Quantum-ready: HUBO problem structure is QUBO-compatible for D-Wave,
IBM Quantum (BF-DCQO), or IonQ via PennyLane/Qiskit adapters.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from ..chat_protocol import get_attr


@dataclass
class HuboProblem:
    """A Higher-Order Unconstrained Binary Optimization problem.

    Represents the combinatorial optimization over which reasoning
    fragments to include in the final aggregated answer.
    """
    n_variables: int  # Number of reasoning fragments (binary variables)
    weights: dict[tuple[int, ...], float] = field(default_factory=dict)
    # weights maps subset tuples to their coefficients:
    #   (i,) -> unary weight (popularity)
    #   (i, j) -> pairwise correlation
    #   (i, j, k) -> higher-order coherence
    labels: list[str] = field(default_factory=list)  # Fragment text for each variable

    def energy(self, x: list[int]) -> float:
        """Compute H(x) for a binary configuration."""
        total = 0.0
        for subset, weight in self.weights.items():
            product = 1
            for idx in subset:
                if idx < len(x):
                    product *= x[idx]
                else:
                    product = 0
                    break
            total += weight * product
        return total

    def to_qubo(self) -> dict[tuple[int, int], float]:
        """Reduce HUBO to QUBO (pairwise) using substitution method.

        Introduces auxiliary variables for higher-order terms.
        For a term w_{ijk} * x_i * x_j * x_k, substitute x_i * x_j -> x_a
        with penalty M * (x_a - 2*x_a*x_i - 2*x_a*x_j + 2*x_i*x_j).
        """
        qubo: dict[tuple[int, int], float] = {}
        aux_var = self.n_variables  # Next available auxiliary variable index
        M = max(abs(w) for w in self.weights.values()) * 2 if self.weights else 10.0

        for subset, weight in self.weights.items():
            if len(subset) == 1:
                # Unary term
                key = (subset[0], subset[0])
                qubo[key] = qubo.get(key, 0.0) + weight
            elif len(subset) == 2:
                # Pairwise term
                key = (subset[0], subset[1])
                qubo[key] = qubo.get(key, 0.0) + weight
            elif len(subset) >= 3:
                # Higher-order: reduce iteratively
                remaining = list(subset)
                current_pair = (remaining[0], remaining[1])
                # Create auxiliary variable for this pair
                a = aux_var
                aux_var += 1
                # Penalty terms for substitution
                qubo[(a, a)] = qubo.get((a, a), 0.0) - M * weight
                qubo[(a, current_pair[0])] = qubo.get((a, current_pair[0]), 0.0) + M * weight
                qubo[(a, current_pair[1])] = qubo.get((a, current_pair[1]), 0.0) + M * weight
                qubo[(current_pair[0], current_pair[1])] = qubo.get(
                    (current_pair[0], current_pair[1]), 0.0
                ) - M * weight
                # Remaining variables chain with auxiliary
                for k in remaining[2:]:
                    new_a = aux_var
                    aux_var += 1
                    qubo[(new_a, new_a)] = qubo.get((new_a, new_a), 0.0) - M * weight
                    qubo[(new_a, a)] = qubo.get((new_a, a), 0.0) + M * weight
                    qubo[(new_a, k)] = qubo.get((new_a, k), 0.0) + M * weight
                    qubo[(a, k)] = qubo.get((a, k), 0.0) - M * weight
                    a = new_a

        return qubo


def build_hubo_from_fragments(
    fragments: list[str],
    embeddings: list[list[float]] | None = None,
    popularity: list[float] | None = None,
    order: int = 3,
    correlation_weight: float = 0.5,
    coherence_weight: float = 0.2,
) -> HuboProblem:
    """Build a HUBO problem from reasoning fragments.

    Args:
        fragments: List of reasoning fragment texts.
        embeddings: Optional pre-computed embeddings for correlation computation.
        popularity: Optional pre-computed popularity scores (uniform if not given).
        order: Maximum interaction order (1=unary, 2=pairwise, 3=three-body).
        correlation_weight: Weight for pairwise correlation terms.
        coherence_weight: Weight for higher-order coherence terms.

    Returns:
        A HuboProblem ready for optimization.
    """
    n = len(fragments)
    problem = HuboProblem(n_variables=n, labels=fragments)

    # Unary: popularity
    pop = popularity or [1.0 / n] * n
    for i in range(n):
        problem.weights[(i,)] = pop[i]

    # Pairwise: cosine similarity
    if embeddings and len(embeddings) == n:
        for i in range(n):
            for j in range(i + 1, n):
                sim = _cosine_sim(embeddings[i], embeddings[j])
                if abs(sim) > 0.01:
                    problem.weights[(i, j)] = correlation_weight * sim

    # Higher-order coherence (3-body)
    if order >= 3 and embeddings and len(embeddings) == n:
        for i in range(n):
            for j in range(i + 1, n):
                for k in range(j + 1, n):
                    # Three-body coherence: geometric mean of pairwise similarities
                    sim_ij = _cosine_sim(embeddings[i], embeddings[j])
                    sim_ik = _cosine_sim(embeddings[i], embeddings[k])
                    sim_jk = _cosine_sim(embeddings[j], embeddings[k])
                    coherence = math.pow(max(0, sim_ij * sim_ik * sim_jk), 1.0 / 3)
                    if coherence > 0.01:
                        problem.weights[(i, j, k)] = coherence_weight * coherence

    return problem


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def simulated_annealing(
    problem: HuboProblem,
    *,
    initial_temp: float = 2.0,
    final_temp: float = 0.01,
    cooling_rate: float = 0.95,
    steps_per_temp: int = 50,
    seed: int | None = None,
) -> tuple[list[int], float]:
    """Solve HUBO using classical simulated annealing.

    Args:
        problem: The HUBO problem.
        initial_temp: Starting temperature.
        final_temp: Stopping temperature.
        cooling_rate: Temperature decay factor.
        steps_per_temp: Monte Carlo steps per temperature.
        seed: Random seed for reproducibility.

    Returns:
        (best_solution, best_energy) where solution is binary vector.
    """
    rng = random.Random(seed)
    n = problem.n_variables

    # Initialize randomly
    current = [rng.randint(0, 1) for _ in range(n)]
    current_energy = problem.energy(current)
    best = list(current)
    best_energy = current_energy

    temp = initial_temp
    while temp > final_temp:
        for _ in range(steps_per_temp):
            # Flip a random variable
            flip_idx = rng.randint(0, n - 1)
            candidate = list(current)
            candidate[flip_idx] = 1 - candidate[flip_idx]
            candidate_energy = problem.energy(candidate)

            delta = candidate_energy - current_energy
            # We MINIMIZE energy (HUBO: lower is better for negative correlation weights)
            # But for QCR-LLM, we want to MAXIMIZE quality, so we negate
            # Convention: positive weights = good, so we maximize energy
            if delta > 0 or rng.random() < math.exp(delta / temp):
                current = candidate
                current_energy = candidate_energy
                if current_energy > best_energy:
                    best = list(current)
                    best_energy = current_energy

        temp *= cooling_rate

    return best, best_energy


@dataclass
class QCRAggregator:
    """QCR-LLM style combinatorial reasoning aggregator.

    Generates multiple CoT fragments from the LLM, embeds them,
    builds a HUBO problem, and solves it to select the best subset.
    """
    n_samples: int = 5
    order: int = 3
    correlation_weight: float = 0.5
    coherence_weight: float = 0.2
    sa_initial_temp: float = 2.0
    sa_cooling_rate: float = 0.95
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None

    def generate_fragments(
        self, task: str, client: Any, model: str, n: int | None = None,
    ) -> list[str]:
        """Generate multiple CoT reasoning fragments."""
        n = n or self.n_samples
        fragments: list[str] = []
        for _ in range(n):
            try:
                response = client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a reasoning agent. Think step by step."},
                        {"role": "user", "content": task},
                    ],
                    stream=False,
                    options={"temperature": 0.7},  # Higher temp for diversity
                )
                text = get_attr(get_attr(response, "message", {}), "content", "").strip()
                if text:
                    fragments.append(text)
            except Exception:
                continue
        return fragments

    def aggregate(self, fragments: list[str]) -> tuple[list[int], float, HuboProblem]:
        """Build HUBO and solve to select the best fragment subset.

        Returns (selected_indices, total_energy, problem).
        """
        embeddings = None
        if self.embed_fn and fragments:
            try:
                embeddings = self.embed_fn(fragments)
            except Exception:
                pass

        problem = build_hubo_from_fragments(
            fragments,
            embeddings=embeddings,
            order=self.order,
            correlation_weight=self.correlation_weight,
            coherence_weight=self.coherence_weight,
        )

        solution, energy = simulated_annealing(
            problem,
            initial_temp=self.sa_initial_temp,
            cooling_rate=self.sa_cooling_rate,
        )
        selected = [i for i, x in enumerate(solution) if x == 1]
        return selected, energy, problem


def run_qcr_aggregation(
    *,
    task: str,
    client: Any,
    model: str,
    n_samples: int = 5,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    order: int = 3,
) -> tuple[str, list[str], dict[str, Any]]:
    """Run a complete QCR-LLM aggregation pipeline.

    Args:
        task: The reasoning task.
        client: Ollama client.
        model: Model name for fragment generation.
        n_samples: Number of CoT fragments to generate.
        embed_fn: Optional embedding function for correlation computation.
        order: Maximum HUBO interaction order.

    Returns:
        (aggregated_answer, selected_fragments, metadata)
    """
    aggregator = QCRAggregator(n_samples=n_samples, embed_fn=embed_fn, order=order)

    # 1. Generate diverse reasoning fragments
    fragments = aggregator.generate_fragments(task, client, model)
    if not fragments:
        return "(no fragments generated)", [], {"error": "generation_failed"}

    # 2. Build and solve HUBO
    selected_indices, energy, problem = aggregator.aggregate(fragments)
    selected_fragments = [fragments[i] for i in selected_indices if i < len(fragments)]

    # 3. Synthesize selected fragments into a final answer
    if not selected_fragments:
        selected_fragments = fragments[:1]  # Fallback to first fragment

    synthesis_prompt = (
        "Synthesize these reasoning fragments into a single coherent answer:\n\n"
        + "\n\n---\n\n".join(f"Fragment {i+1}:\n{f[:1500]}" for i, f in enumerate(selected_fragments))
    )
    try:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are a reasoning synthesizer. Combine diverse perspectives into a unified answer."},
                {"role": "user", "content": synthesis_prompt},
            ],
            stream=False,
        )
        answer = get_attr(get_attr(response, "message", {}), "content", "").strip()
    except Exception:
        answer = selected_fragments[0] if selected_fragments else "(synthesis failed)"

    metadata = {
        "total_fragments": len(fragments),
        "selected_count": len(selected_fragments),
        "selected_indices": selected_indices,
        "energy": energy,
        "hubo_terms": len(problem.weights),
    }
    return answer, selected_fragments, metadata
