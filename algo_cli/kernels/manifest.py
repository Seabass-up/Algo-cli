"""Manifest and live readiness audit for user-facing Algo CLI kernels.

The registry intentionally stores module paths as strings. A kernel manifest is
discoverable metadata, not a workload launcher. ``preview`` and ``planned``
actions are descriptive capability IDs; every ``active`` action must also have
an ActionSpec so the runtime's risk/approval contract is explicit.

Built-in kernels are the public framework. Each user keeps their own kernels in
``<config dir>/kernels/kernels.json`` with importable modules beside it; those
are loaded on that machine only and are never part of a release.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KernelSpec:
    name: str
    description: str
    modules: list[str]
    actions: list[str]
    slash_commands: list[str]
    safety_level: str
    status: str
    source: str = "built-in"


@dataclass(frozen=True)
class UserKernelCatalog:
    path: Path
    kernels: tuple[KernelSpec, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class KernelAudit:
    name: str
    declared_status: str
    health: str
    modules_checked: int
    slash_commands_checked: int
    registered_actions: int
    total_actions: int
    issues: tuple[str, ...]


_KERNELS: tuple[KernelSpec, ...] = (
    KernelSpec(
        name="jev",
        description="Bounded advisory Jev question contracts with local lint, review, run and followup.",
        modules=["algo_cli.jev_kernel"],
        actions=["jev.status", "jev.question_contract"],
        slash_commands=["/kernel show jev", "/kernel check jev"],
        safety_level="medium",
        status="active",
    ),
    KernelSpec(
        name="benchmark",
        description="Benchmark and compare agent/runtime behavior with repeatable evaluation metadata.",
        modules=[
            "algo_cli.intelligence.agent_benchmark",
            "algo_cli.intelligence.agent_arena",
            "algo_cli.evals.harness_retrieval_benchmark",
            "algo_cli.evals.algorithm_effectiveness",
            "algo_cli.evals.competitive_harness_rating",
        ],
        actions=[
            "benchmark.compare",
            "benchmark.report",
            "benchmark.algorithm_effectiveness",
        ],
        slash_commands=["/harness score", "/harness compare", "/kernel show benchmark"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="repo-intelligence",
        description="Inspect repository structure, project graphs, code search signals, and local GraphRAG context.",
        modules=[
            "algo_cli.intelligence.project_graph",
            "algo_cli.intelligence.graph_rag",
            "algo_cli.intelligence.code_graph",
            "algo_cli.intelligence.coderank",
            "algo_cli.intelligence.repo_map",
            "algo_cli.code_rag",
        ],
        actions=["repo.status", "repo.query", "repo.reindex"],
        slash_commands=["/intelligence status", "/intelligence query TERM", "/intel query TERM"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="harness-lexical-index",
        description="Build and query a first-stage lexical harness index, starting with SQLite FTS5 and leaving Tantivy as a Rust-side upgrade path.",
        modules=[
            "algo_cli.harness",
            "algo_cli.code_rag",
            "algo_cli.intelligence.index_store",
        ],
        actions=["harness.lexical.build", "harness.lexical.query"],
        slash_commands=["/kernel show harness-lexical-index"],
        safety_level="low",
        status="planned",
    ),
    KernelSpec(
        name="harness-vector-ann",
        description="Build and query an approximate-nearest-neighbor sidecar for harness embeddings, with the current NumPy scorer as fallback.",
        modules=[
            "algo_cli.harness",
            "algo_cli.code_rag",
            "algo_cli.intelligence.index_store",
        ],
        actions=["harness.vector.build", "harness.vector.query"],
        slash_commands=["/kernel show harness-vector-ann"],
        safety_level="low",
        status="planned",
    ),
    KernelSpec(
        name="harness-fusion-ranker",
        description="Rank with BM25, fuse exact-vector evidence with RRF, and expose component provenance.",
        modules=[
            "algo_cli.harness",
            "algo_cli.retrieval_algorithms",
            "algo_cli.intelligence.query_expansion",
        ],
        actions=["harness.fusion.lexical_rank", "harness.fusion.rank", "harness.fusion.explain"],
        slash_commands=["/kernel show harness-fusion-ranker"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="harness-context-pack",
        description="Pack ranked harness results with MMR-style deduplication and token-budget selection before model context injection.",
        modules=[
            "algo_cli.harness",
            "algo_cli.context_budget",
            "algo_cli.intelligence.context_ops",
        ],
        actions=["harness.context.pack", "harness.context.explain"],
        slash_commands=["/kernel show harness-context-pack"],
        safety_level="low",
        status="planned",
    ),
    KernelSpec(
        name="harness-incremental-index",
        description="Reuse unchanged harness records and content-identical code chunk embeddings before rebuilding changed sidecars.",
        modules=[
            "algo_cli.harness",
            "algo_cli.code_rag",
            "algo_cli.intelligence.index_store",
        ],
        actions=["harness.index.refresh_changed", "harness.index.status"],
        slash_commands=["/kernel show harness-incremental-index"],
        safety_level="medium",
        status="active",
    ),
    KernelSpec(
        name="harness-rerank",
        description="Optionally rerank bounded top-N harness candidates with a cross-encoder or local scoring model after cheap retrieval.",
        modules=[
            "algo_cli.harness",
            "algo_cli.intelligence.query_expansion",
        ],
        actions=["harness.rerank.candidates", "harness.rerank.explain"],
        slash_commands=["/kernel show harness-rerank"],
        safety_level="low",
        status="planned",
    ),
    KernelSpec(
        name="review",
        description="Produce structured review findings, risk summaries, and implementation critique signals.",
        modules=[
            "algo_cli.intelligence.critic_loop",
            "algo_cli.intelligence.structural_validator",
            "algo_cli.intelligence.actionability",
            "algo_cli.intelligence.golden_master",
        ],
        actions=["review.findings", "review.risk_summary"],
        slash_commands=["/kernel show review"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="document-ingest",
        description="Normalize and extract content from documents into source records for downstream analysis.",
        modules=[
            "algo_cli.intelligence.document_ingest",
            "algo_cli.intelligence.content_extractor",
            "algo_cli.intelligence.source_registry",
        ],
        actions=["document.extract", "document.normalize"],
        slash_commands=["/kernel show document-ingest"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="research",
        description="Coordinate source gathering, query expansion, and cross-source research workspace metadata.",
        modules=[
            "algo_cli.intelligence.deep_research",
            "algo_cli.intelligence.cross_source",
            "algo_cli.intelligence.research_workspace",
            "algo_cli.intelligence.query_expansion",
            "algo_cli.intelligence.gatherer",
        ],
        actions=["research.gather", "research.synthesize"],
        slash_commands=["/kernel show research"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="agent-runtime",
        description="Run traceable pipelines and bounded read-only specialist teams with persistent parent/child handoffs.",
        modules=[
            "algo_cli.agent_pipeline",
            "algo_cli.agent_threads",
            "algo_cli.intelligence.agent_runtime",
            "algo_cli.intelligence.agents_as_tools",
            "algo_cli.intelligence.autonomous_engineer",
            "algo_cli.intelligence.team_execution",
            "algo_cli.intelligence.subagent_spawner",
        ],
        actions=["agent.plan", "agent.delegate", "agent.report", "agent.thread.resume"],
        slash_commands=[
            "/agent [--pipeline NAME] TASK",
            "/agent team [--roles A,B[,C,D]] TASK",
            "/agent threads",
            "/kernel show agent-runtime",
        ],
        safety_level="medium",
        status="active",
    ),
    KernelSpec(
        name="flow-dag",
        description="Represent workflow DAGs, orchestration edges, and saga-style recovery metadata.",
        modules=[
            "algo_cli.intelligence.flow_dag",
            "algo_cli.intelligence.dag_orchestration",
            "algo_cli.intelligence.saga_pattern",
        ],
        actions=["flow.describe", "flow.validate"],
        slash_commands=["/kernel show flow-dag"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="extension",
        description="Describe extension manifests, extension hosts, and kernel plugin metadata.",
        modules=[
            "algo_cli.intelligence.extension_manifest",
            "algo_cli.intelligence.extension_host",
            "algo_cli.intelligence.kernel_plugins",
        ],
        actions=["extension.inspect", "extension.validate"],
        slash_commands=["/kernel show extension"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="echo-fidelity-guard",
        description="Distinguish None (unmeasured) from 0.0 (measured failure) from valid float — prevents fabrications.",
        modules=["algo_cli.intelligence.echo_fidelity"],
        actions=["guard.echo_fidelity"],
        slash_commands=["/kernel show echo-fidelity-guard"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="numeric-clamp-guard",
        description="Clamp telemetry values to a valid range and record warnings when out-of-range values are encountered.",
        modules=["algo_cli.intelligence.numeric_clamp"],
        actions=["guard.numeric_clamp"],
        slash_commands=["/kernel show numeric-clamp-guard"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="ema-tuning",
        description="Feedback-driven parameter tuning via exponential moving average with sample-count gating and bounded history.",
        modules=["algo_cli.intelligence.ema_tuning"],
        actions=["tuning.update", "tuning.apply"],
        slash_commands=["/kernel show ema-tuning"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="bonferroni-guard",
        description="Bonferroni correction for multiple comparisons — prevents false discoveries when running many tests.",
        modules=["algo_cli.intelligence.bonferroni"],
        actions=["guard.bonferroni"],
        slash_commands=["/kernel show bonferroni-guard"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="worktree-isolation",
        description="Create collision-safe Git worktrees and bind durable agent threads to verified repository state.",
        modules=[
            "algo_cli.worktree_runtime",
            "algo_cli.agent_threads",
            "algo_cli.agent_pipeline",
        ],
        actions=[
            "worktree.inspect",
            "worktree.create",
            "worktree.activate",
            "worktree.remove",
        ],
        slash_commands=[
            "/worktree status",
            "/worktree new NAME",
            "/agent switch THREAD",
            "/agent fork THREAD TASK",
        ],
        safety_level="high",
        status="active",
    ),
    KernelSpec(
        name="pre-push-gate",
        description="Guard structured commit, push, and draft-PR phases with fingerprints, scrubs, and remote freshness checks.",
        modules=["algo_cli.intelligence.pre_push_gate", "algo_cli.git_publish"],
        actions=["gate.pre_push", "ship.plan", "ship.execute"],
        slash_commands=["/ship status", "/ship all MESSAGE", "/kernel show pre-push-gate"],
        safety_level="high",
        status="active",
    ),
    # --- Phase 2: Medium deterministic kernels (H1, H3, H4, H9, H11) ---
    KernelSpec(
        name="finding-record",
        description="Structured append-only finding records with provenance, severity, and lifecycle tracking.",
        modules=["algo_cli.intelligence.finding_record"],
        actions=["finding.create", "finding.query", "finding.update"],
        slash_commands=["/kernel show finding-record"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="retraction-ledger",
        description="Append-only retraction records — never silent deletes, always leave a trace.",
        modules=["algo_cli.intelligence.retraction_ledger"],
        actions=["retraction.add", "retraction.list"],
        slash_commands=["/kernel show retraction-ledger"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="discovery-event-log",
        description="Structured events for the full discovery lifecycle: discovered, proposed, verified, retracted.",
        modules=["algo_cli.intelligence.event_log"],
        actions=["event.emit", "event.query"],
        slash_commands=["/kernel show discovery-event-log"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="artifact-binding",
        description="Bind every claim to a raw artifact with content hash for provenance verification.",
        modules=["algo_cli.intelligence.artifact_binding"],
        actions=["binding.bind", "binding.verify"],
        slash_commands=["/kernel show artifact-binding"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="checkpoint-resume",
        description="Resumable long catalog operations via state serialization and checkpoint recovery.",
        modules=["algo_cli.intelligence.checkpoint_resume"],
        actions=["checkpoint.save", "checkpoint.load", "checkpoint.list"],
        slash_commands=["/kernel show checkpoint-resume"],
        safety_level="low",
        status="preview",
    ),
    # --- Phase 2: Medium deterministic kernels (H14, H16, H17, H19, H21, H22, H27, H30, H31, H2) ---
    KernelSpec(
        name="symmetric-verify",
        description="Every creator pattern needs a verifier companion — graph analysis of encode/decode pairs.",
        modules=["algo_cli.intelligence.symmetric_verify"],
        actions=["verify.symmetric"],
        slash_commands=["/kernel show symmetric-verify"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="circuit-breaker",
        description="Accumulate risk per failure, auto-disable at threshold — prevents cascading failures.",
        modules=["algo_cli.intelligence.circuit_breaker"],
        actions=["breaker.check", "breaker.reset", "breaker.record"],
        slash_commands=["/kernel show circuit-breaker"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="negative-controls",
        description="Known-clean samples to measure false positive rate — confusion matrix for catalog verification.",
        modules=["algo_cli.intelligence.negative_controls"],
        actions=["verify.negative_controls"],
        slash_commands=["/kernel show negative-controls"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="stat-stability-guard",
        description="Warn when sample sizes are too small for stable results — t-distribution CI calculation.",
        modules=["algo_cli.intelligence.stat_stability"],
        actions=["guard.stat_stability"],
        slash_commands=["/kernel show stat-stability-guard"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="context-adaptive",
        description="Classify context via weighted pattern scoring, then select parameters before execution.",
        modules=["algo_cli.intelligence.context_adaptive"],
        actions=["tuning.detect_context"],
        slash_commands=["/kernel show context-adaptive"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="output-normalize",
        description="Toggleable pipeline stages for sequential output normalization (hedge reduction, direct mode, casual mode).",
        modules=["algo_cli.intelligence.output_normalize"],
        actions=["normalize.output"],
        slash_commands=["/kernel show output-normalize"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="degenerate-detector",
        description="Detect solutions that precompute answers without required control flow (loops, functions, conditionals).",
        modules=["algo_cli.intelligence.degenerate_detector"],
        actions=["verify.degenerate"],
        slash_commands=["/kernel show degenerate-detector"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="delta-report",
        description="Report what changed since last assessment — set operations on old vs new state with priority classification.",
        modules=["algo_cli.intelligence.delta_report"],
        actions=["report.delta"],
        slash_commands=["/kernel show delta-report"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="tiered-access-gate",
        description="Tiered tool access: default (always granted), opt-in (env var gated), approval-gated (env var + human approval).",
        modules=["algo_cli.intelligence.tiered_access"],
        actions=["gate.check_access"],
        slash_commands=["/kernel show tiered-access-gate"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="catalog-verifier",
        description="Re-derive every 'implemented' status from live tests — parse ALGO.md, run tests, report discrepancies.",
        modules=["algo_cli.intelligence.catalog_verifier"],
        actions=["catalog.verify"],
        slash_commands=["/kernel show catalog-verifier"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="lesson-catalog-propose",
        description="Scan lesson text for algorithmic patterns and propose new catalog entries.",
        modules=["algo_cli.intelligence.lesson_catalog"],
        actions=["propose.lesson_catalog"],
        slash_commands=["/kernel show lesson-catalog-propose"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="adversarial-audit",
        description="Run tests and compare claimed vs actual behavior to catch fabricated or exaggerated claims.",
        modules=["algo_cli.intelligence.adversarial_audit"],
        actions=["audit.adversarial"],
        slash_commands=["/kernel show adversarial-audit"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="multi-model-score",
        description="Score algorithm candidates across a panel of models and pick the winner.",
        modules=["algo_cli.intelligence.multi_model_score"],
        actions=["score.multi_model"],
        slash_commands=["/kernel show multi-model-score"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="llm-fallback-chain",
        description="Resilient LLM call with primary → fallback model → 3-tier JSON parsing.",
        modules=["algo_cli.intelligence.llm_fallback"],
        actions=["guard.llm_fallback"],
        slash_commands=["/kernel show llm-fallback-chain"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="dual-layer-validate",
        description="Two-layer validation: validator probes check claims, independent skeptic attempts refutation.",
        modules=["algo_cli.intelligence.dual_layer_validate"],
        actions=["validate.dual_layer"],
        slash_commands=["/kernel show dual-layer-validate"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="falsification-suite",
        description="Multiple independent probes (S1–S4) that attack a claim from different angles.",
        modules=["algo_cli.intelligence.falsification_suite"],
        actions=["validate.falsification"],
        slash_commands=["/kernel show falsification-suite"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="consortium-synthesis",
        description="Synthesize ground truth from all model responses — ground truth over popularity, specificity wins.",
        modules=["algo_cli.intelligence.consortium_synthesis"],
        actions=["synthesize.consortium"],
        slash_commands=["/kernel show consortium-synthesis"],
        safety_level="medium",
        status="preview",
    ),
    KernelSpec(
        name="multi-tier-grade",
        description="Three grading tiers: strict (unforgiving), lenient (surface recovery), structured (requires constructs).",
        modules=["algo_cli.intelligence.multi_tier_grade"],
        actions=["grade.multi_tier"],
        slash_commands=["/kernel show multi-tier-grade"],
        safety_level="low",
        status="preview",
    ),
    # --- Phase 4: Pattern-derived kernels (Fable-5 corpus + macOS /System audit) ---
    KernelSpec(
        name="policy-chain",
        description="PAM-style policy chain: ordered checks with control flags (required / sufficient / requisite / include). Uses scoped authority and an audit-trail-preserving decision model.",
        modules=[
            "algo_cli._internal.policy_chain",
            "algo_cli.samuel_policy",
        ],
        actions=["chain.evaluate", "chain.audit"],
        slash_commands=["/kernel show policy-chain"],
        safety_level="medium",
        status="active",
    ),
    KernelSpec(
        name="cot-quality-scorer",
        description="Score CoT blocks on sequencing markers, length ratio, and verification cadence. Flags under-thinking, over-thinking, and stream-of-consciousness reasoning. Calibrated against the Fable-5 corpus (4,665 rows; median cot_ratio = 1.14).",
        modules=["algo_cli.evals.cot_quality"],
        actions=["cot.score"],
        slash_commands=["/kernel show cot-quality-scorer"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="bash-description-discipline",
        description="Require a non-empty description field on every Bash tool call. 100% of Bash calls in the Fable-5 corpus (1544/1544) had descriptions; replicate the discipline as a tool gate.",
        modules=["algo_cli.tools"],
        actions=["bash.validate_description"],
        slash_commands=["/kernel show bash-description-discipline"],
        safety_level="low",
        status="preview",
    ),
    KernelSpec(
        name="screenshot-verification",
        description="Turn vision screenshot descriptions into structured expected/forbidden UI evidence checks.",
        modules=["algo_cli.vision_screenshot_verify", "algo_cli.tools"],
        actions=["vision.screenshot_verify"],
        slash_commands=["/kernel show screenshot-verification"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="tool-sequence-tdd",
        description="Detect healthy Edit→Bash→Edit and verification-after-edit tool cadences from Fable-5 traces.",
        modules=["algo_cli.evals.cot_quality"],
        actions=["tool_sequence.score"],
        slash_commands=["/kernel show tool-sequence-tdd"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="session-distribution",
        description="Summarize session row distributions and flag heavy-tail concentration risk.",
        modules=["algo_cli.evals.session_distribution", "algo_cli.harness"],
        actions=["session_distribution.summarize"],
        slash_commands=["/kernel show session-distribution"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="runtime-qos",
        description="Classify runtime posture, attach named logs, and deterministically order bounded tool batches.",
        modules=["algo_cli.theodore_runtime_qos", "algo_cli.harness", "algo_cli.tools"],
        actions=["runtime.qos.classify", "runtime.qos.schedule", "runtime.log_path"],
        slash_commands=["/kernel show runtime-qos"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="window-tinylfu",
        description="Bounded Window TinyLFU admission for harness and identity query-embedding caches.",
        modules=["algo_cli.cache_admission", "algo_cli.harness", "algo_cli.identity"],
        actions=["cache.tinylfu.admit", "cache.tinylfu.stats"],
        slash_commands=["/kernel show window-tinylfu"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="performance-cusum",
        description="Detect sustained comparable-series latency shifts and report them through /selfcheck.",
        modules=["algo_cli.evals.performance_regression", "algo_cli.dorothy_perf_telemetry"],
        actions=["performance.cusum.detect", "performance.cusum.selfcheck"],
        slash_commands=["/selfcheck", "/kernel show performance-cusum"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="capability-mask",
        description="Stable numeric capability bit masks for tool/kernel tiers, inspired by macOS audit_class.",
        modules=["algo_cli.marcus_authority", "algo_cli.samuel_policy"],
        actions=["capability.mask", "capability.tier"],
        slash_commands=["/kernel show capability-mask"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="extensions-manifest",
        description="SystemVersion-style extension manifest for plugins and helper binaries.",
        modules=["algo_cli.argon_extensions_manifest", "algo_cli.version_manifest", "algo_cli.tools"],
        actions=["extensions.manifest"],
        slash_commands=["/kernel show extensions-manifest"],
        safety_level="low",
        status="active",
    ),
    KernelSpec(
        name="small-context-ledger",
        description="For models with runtime context below 75k, write full optional context to a temp ledger file and inject a compact refresh trigger so small models can regain context on demand.",
        modules=["algo_cli.small_context", "algo_cli.context_budget", "algo_cli.main", "algo_cli.tools"],
        actions=["small_context.ledger.write", "small_context.ledger.preview"],
        slash_commands=["/kernel show small-context-ledger"],
        safety_level="low",
        status="active",
    ),
)


def _copy_spec(spec: KernelSpec) -> KernelSpec:
    return KernelSpec(
        name=spec.name,
        description=spec.description,
        modules=list(spec.modules),
        actions=list(spec.actions),
        slash_commands=list(spec.slash_commands),
        safety_level=spec.safety_level,
        status=spec.status,
        source=spec.source,
    )


USER_KERNELS_FILENAME = "kernels.json"
MAX_USER_KERNELS_BYTES = 256 * 1024
MAX_USER_KERNELS = 200
MAX_USER_KERNEL_TEXT = 1024
_USER_SPEC_FIELDS = ("name", "description", "modules", "actions", "slash_commands", "safety_level", "status")


def user_kernels_dir() -> Path:
    from .. import config

    return Path(config.CONFIG_DIR) / "kernels"


def _string_list(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or not all(isinstance(item, str) and item.strip() and len(item) <= MAX_USER_KERNEL_TEXT for item in value)
    ):
        return None
    return [item.strip() for item in value]


def load_user_kernels() -> UserKernelCatalog:
    """Read this machine's personal kernel catalog; absent means empty, malformed entries are skipped."""

    directory = user_kernels_dir()
    path = directory / USER_KERNELS_FILENAME
    if not path.is_file():
        return UserKernelCatalog(path, (), ())
    try:
        # Read one byte past the limit so an oversized file is refused before parsing.
        with path.open("rb") as handle:
            raw = handle.read(MAX_USER_KERNELS_BYTES + 1)
        if len(raw) > MAX_USER_KERNELS_BYTES:
            return UserKernelCatalog(path, (), (f"{path.name}: larger than {MAX_USER_KERNELS_BYTES} bytes",))
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        return UserKernelCatalog(path, (), (f"{path.name}: unreadable: {type(exc).__name__}",))
    rows = payload.get("kernels") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return UserKernelCatalog(path, (), (f'{path.name}: expected {{"kernels": [...]}}',))
    if len(rows) > MAX_USER_KERNELS:
        return UserKernelCatalog(path, (), (f"{path.name}: more than {MAX_USER_KERNELS} kernels",))
    taken = {spec.name for spec in _KERNELS}
    specs: list[KernelSpec] = []
    issues: list[str] = []
    for index, row in enumerate(rows):
        label = f"kernels[{index}]"
        if not isinstance(row, dict):
            issues.append(f"{label}: expected an object")
            continue
        name = str(row.get("name") or "").strip().lower()
        if not name:
            issues.append(f"{label}: missing name")
            continue
        if len(name) > 64:
            issues.append(f"{label}: name is longer than 64 characters")
            continue
        if name in taken:
            issues.append(f"{name}: name is already registered")
            continue
        unknown = sorted(str(key)[:64] for key in set(row) - set(_USER_SPEC_FIELDS))
        lists = {key: _string_list(row.get(key, [])) for key in ("modules", "actions", "slash_commands")}
        bad = [key for key, value in lists.items() if value is None]
        description = row.get("description", "")
        if unknown or bad or not isinstance(description, str) or len(description) > MAX_USER_KERNEL_TEXT:
            issues.append(f"{name}: invalid fields: {', '.join(unknown + bad) or 'description'}")
            continue
        taken.add(name)
        specs.append(
            KernelSpec(
                name=name,
                description=str(row.get("description") or "").strip(),
                modules=lists["modules"] or [],
                actions=lists["actions"] or [],
                slash_commands=lists["slash_commands"] or [],
                safety_level=str(row.get("safety_level") or "medium"),
                status=str(row.get("status") or "preview"),
                source="user",
            )
        )
    if specs:
        # Appended, never prepended: a personal module cannot shadow the runtime or stdlib.
        entry = str(directory)
        if entry not in sys.path:
            sys.path.append(entry)
    return UserKernelCatalog(path, tuple(specs), tuple(issues))


def _all_kernels() -> tuple[KernelSpec, ...]:
    return _KERNELS + load_user_kernels().kernels


def list_kernels() -> list[KernelSpec]:
    return [_copy_spec(spec) for spec in _all_kernels()]


def get_kernel(name: str) -> KernelSpec | None:
    normalized = (name or "").strip().lower()
    for spec in _all_kernels():
        if spec.name == normalized:
            return _copy_spec(spec)
    return None


def kernel_names() -> list[str]:
    return [spec.name for spec in _all_kernels()]


def kernel_runtime_snapshot() -> dict[str, Any]:
    """Return the live kernel catalog for runtime discovery surfaces."""

    specs = list_kernels()
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.status] = counts.get(spec.status, 0) + 1
    return {
        "total": len(specs),
        "counts": counts,
        "kernels": [
            {
                "name": spec.name,
                "status": spec.status,
                "safety_level": spec.safety_level,
                "description": spec.description,
                "source": spec.source,
            }
            for spec in specs
        ],
    }


def audit_kernel(spec: KernelSpec) -> KernelAudit:
    """Validate imports, slash roots, metadata, and declared action contracts.

    This is a declaration audit, not a dry-run execution probe.
    """

    from ..action_registry import list_action_specs
    from ..oliver_slash_dispatch import SLASH_COMMANDS

    issues: list[str] = []
    if spec.status not in {"planned", "preview", "active"}:
        issues.append(f"invalid status: {spec.status}")
    if spec.safety_level not in {"low", "medium", "high"}:
        issues.append(f"invalid safety level: {spec.safety_level}")
    if len(spec.modules) != len(set(spec.modules)):
        issues.append("duplicate module declarations")
    if len(spec.actions) != len(set(spec.actions)):
        issues.append("duplicate action declarations")
    if len(spec.slash_commands) != len(set(spec.slash_commands)):
        issues.append("duplicate slash command declarations")

    for module_path in spec.modules:
        try:
            importlib.import_module(module_path)
        except Exception as exc:
            issues.append(f"module {module_path}: {type(exc).__name__}: {exc}")

    slash_roots = {command.split()[0] for command, _description in SLASH_COMMANDS}
    for command in spec.slash_commands:
        root = command.split()[0] if command.split() else ""
        if root not in slash_roots:
            issues.append(f"slash root is not registered: {command}")

    action_specs = {
        action.name
        for action in list_action_specs(include_archived=False)
        if action.kind == "kernel"
    }
    registered = sum(action in action_specs for action in spec.actions)
    if spec.status == "active":
        missing = [action for action in spec.actions if action not in action_specs]
        if missing:
            issues.append(f"active actions missing ActionSpec: {', '.join(missing)}")

    health = "blocked" if issues else "ready" if spec.status == "active" else spec.status
    return KernelAudit(
        name=spec.name,
        declared_status=spec.status,
        health=health,
        modules_checked=len(spec.modules),
        slash_commands_checked=len(spec.slash_commands),
        registered_actions=registered,
        total_actions=len(spec.actions),
        issues=tuple(issues),
    )


def audit_kernels(name: str | None = None) -> list[KernelAudit]:
    """Audit one kernel or the full manifest without executing workloads."""

    if name:
        spec = get_kernel(name)
        if spec is None:
            raise KeyError(f"Unknown kernel: {name}")
        return [audit_kernel(spec)]
    return [audit_kernel(spec) for spec in list_kernels()]


def render_kernel_audit(audits: list[KernelAudit]) -> str:
    blocked = sum(audit.health == "blocked" for audit in audits)
    ready = sum(audit.health == "ready" for audit in audits)
    preview = sum(audit.health == "preview" for audit in audits)
    planned = sum(audit.health == "planned" for audit in audits)
    lines = [
        f"Kernel audit: {len(audits)} checked · {ready} ready · {preview} preview · "
        f"{planned} planned · {blocked} blocked"
    ]
    if len(audits) > 1:
        ready_names = [audit.name for audit in audits if audit.health == "ready"]
        if ready_names:
            lines.append(
                "- active contract-ready (imports, slash roots, and action declarations): "
                f"{', '.join(ready_names)}"
            )
        if preview:
            lines.append(
                f"- {preview} preview kernels: modules and slash routes resolve; "
                "unregistered actions remain descriptive"
            )
        if planned:
            lines.append(f"- {planned} planned kernels: contracts resolve but are not active runtime surfaces")
        for audit in audits:
            if audit.health != "blocked":
                continue
            lines.append(f"- {audit.name}: blocked")
            lines.extend(f"    issue: {issue}" for issue in audit.issues)
        lines.append("Use /kernel check NAME for per-kernel action coverage and details.")
        return "\n".join(lines)
    for audit in audits:
        lines.append(
            f"- {audit.name}: {audit.health} "
            f"({audit.modules_checked} modules, {audit.slash_commands_checked} slash routes, "
            f"{audit.registered_actions}/{audit.total_actions} registered actions)"
        )
        for issue in audit.issues:
            lines.append(f"    issue: {issue}")
        if not audit.issues and audit.declared_status != "active" and audit.registered_actions < audit.total_actions:
            lines.append("    note: actions are descriptive until this kernel is promoted to active")
        elif not audit.issues and audit.declared_status == "active":
            lines.append("    note: contract audit passed; workload execution is verified by focused tests")
    return "\n".join(lines)
