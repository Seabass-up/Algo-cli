"""Advanced reasoning algorithms for Algo CLI agent harness.

Integrates classical and quantum-inspired reasoning methods:
- ReAct+: Enhanced reasoning-action loops with structured observation parsing
- Reflexion+: Verbal self-critique with episodic memory and retry
- Tree-of-Thoughts (ToT): BFS/DFS/MCTS evaluation with backtracking
- Graph-of-Thoughts (GoT): DAG reasoning with merge/feedback/distill
- QCR-LLM: Quantum-inspired combinatorial reasoning (HUBO/SA)
- Neuro-Symbolic: LLM propose + symbolic solver verification

All algorithms are local-first and work with Ollama models.
Quantum-inspired methods use classical simulated annealing by default
with optional QPU offload via cloud providers.
"""

from .react import ReactLoop, ReactStep, run_react_loop
from .reflexion import ReflexionLoop, ReflexionEpisode, run_reflexion_loop
from .tree_of_thought import TreeOfThought, ThoughtNode, run_tot
from .graph_of_thought import GraphOfThought, ThoughtVertex, run_got
from .combinatorial import QCRAggregator, HuboProblem, run_qcr_aggregation
from .neuro_symbolic import NeuroSymbolicVerifier, VerificationResult, run_neuro_symbolic
from .mcts import MCTSReasoner, MCTSNode, run_mcts

__all__ = [
    "ReactLoop",
    "ReactStep",
    "run_react_loop",
    "ReflexionLoop",
    "ReflexionEpisode",
    "run_reflexion_loop",
    "TreeOfThought",
    "ThoughtNode",
    "run_tot",
    "GraphOfThought",
    "ThoughtVertex",
    "run_got",
    "QCRAggregator",
    "HuboProblem",
    "run_qcr_aggregation",
    "NeuroSymbolicVerifier",
    "VerificationResult",
    "run_neuro_symbolic",
    "MCTSReasoner",
    "MCTSNode",
    "run_mcts",
]
