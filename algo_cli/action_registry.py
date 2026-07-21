"""Defensive ActionSpec registry and doctor checks for Algo CLI.

This registry is a first-pass source of truth for action/tool readiness,
approval risk, provider dependencies, and legacy visibility. It is diagnostic:
it does not weaken approval gates or execute actions.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import inspect
import textwrap
from dataclasses import asdict, dataclass, field

from .config import load_runtime_env
from .marcus_authority import (
    Capability,
    ConfirmationMode,
    DataClass,
    EffectClass,
    IdempotencyClass,
    OutcomeModel,
    RuntimeClass,
    TargetScope,
    VerificationRequirement,
    capability_mask,
    policy_for_action,
)
from typing import Any, Literal, cast
from urllib.error import URLError
from urllib.request import urlopen

RiskLevel = Literal["low", "medium", "high"]
FindingStatus = Literal["ready", "degraded", "blocked"]
ActionKind = Literal["tool", "slash", "provider", "legacy", "kernel"]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    kind: ActionKind
    description: str
    group: str
    tags: tuple[str, ...]
    threat_detection_use: str
    risk_level: RiskLevel
    mutates_state: bool
    requires_approval: bool
    safe_retry: bool
    effect_class: EffectClass = EffectClass.UNCLASSIFIED
    confirmation_mode: ConfirmationMode = ConfirmationMode.HANDOFF_REQUIRED
    data_classes: tuple[DataClass, ...] = (DataClass.SENSITIVE,)
    target_scope: TargetScope = TargetScope.EXTERNAL_STATE
    capabilities: tuple[Capability, ...] = (Capability.UNCLASSIFIED,)
    capability_mask: int = Capability.UNCLASSIFIED.value
    idempotency: IdempotencyClass = IdempotencyClass.NON_IDEMPOTENT
    outcome_model: OutcomeModel = OutcomeModel.UNKNOWN_POSSIBLE
    verification: VerificationRequirement = VerificationRequirement.INDEPENDENT_POSTCONDITION
    fallback_group: str = ""
    curated: bool = False
    dynamic_resolution: bool = False
    runtime_class: RuntimeClass = RuntimeClass.ADAPTIVE
    estimated_cost: float = 2.0
    log_suppression: bool = False
    compensation_action: str = ""
    requires_network: bool = False
    requires_provider: str | None = None
    requires_binary: tuple[str, ...] = ()
    supported_os: tuple[str, ...] = ("windows", "linux", "macos")
    prerequisites: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    archived: bool = False
    archived_reason: str = ""
    replacement: str = ""

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["effect_class"] = self.effect_class.value
        value["confirmation_mode"] = self.confirmation_mode.value
        value["data_classes"] = [item.value for item in self.data_classes]
        value["target_scope"] = self.target_scope.value
        value["capabilities"] = [item.name.lower() for item in self.capabilities]
        value["idempotency"] = self.idempotency.value
        value["outcome_model"] = self.outcome_model.value
        value["verification"] = self.verification.value
        value["runtime_class"] = self.runtime_class.value
        return value


@dataclass(frozen=True)
class DoctorFinding:
    status: FindingStatus
    area: str
    message: str
    recommendation: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class DoctorReport:
    overall_status: FindingStatus
    findings: tuple[DoctorFinding, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


def _spec(
    name: str,
    kind: ActionKind,
    description: str,
    group: str,
    tags: tuple[str, ...],
    threat_detection_use: str,
    risk_level: RiskLevel,
    mutates_state: bool,
    requires_approval: bool,
    safe_retry: bool,
    **kwargs: Any,
) -> ActionSpec:
    policy = policy_for_action(name) if kind == "tool" else None
    if policy is not None:
        risk_level = cast(RiskLevel, policy.maximum_risk)
        mutates_state = policy.mutates_state
        requires_approval = policy.requires_approval
        safe_retry = policy.safe_retry
        authority_fields: dict[str, Any] = {
            "effect_class": policy.effect_class,
            "confirmation_mode": policy.confirmation_mode,
            "data_classes": policy.data_classes,
            "target_scope": policy.target_scope,
            "capabilities": policy.capabilities,
            "capability_mask": policy.capability_mask.value,
            "idempotency": policy.idempotency,
            "outcome_model": policy.outcome_model,
            "verification": policy.verification,
            "fallback_group": policy.fallback_group,
            "curated": policy.curated,
            "dynamic_resolution": policy.dynamic_resolution,
            "runtime_class": policy.runtime_class,
            "estimated_cost": policy.estimated_cost,
            "log_suppression": policy.suppress_logs,
            "compensation_action": policy.compensation_action,
        }
    else:
        legacy_capabilities = (Capability.READ, Capability.WRITE) if mutates_state else (Capability.READ,)
        authority_fields = {
            "effect_class": EffectClass.LOCAL_MUTATION if mutates_state else EffectClass.OBSERVE,
            "confirmation_mode": ConfirmationMode.ACTION_TIME if requires_approval else ConfirmationMode.NONE,
            "data_classes": (DataClass.LOCAL_CONTENT,),
            "target_scope": TargetScope.RUNTIME,
            "capabilities": legacy_capabilities,
            "capability_mask": capability_mask(legacy_capabilities).value,
            "idempotency": IdempotencyClass.IDEMPOTENT if safe_retry else IdempotencyClass.NON_IDEMPOTENT,
            "outcome_model": OutcomeModel.DETERMINISTIC,
            "verification": VerificationRequirement.STRUCTURED_RESULT,
            "fallback_group": "",
            "curated": True,
            "dynamic_resolution": False,
            "runtime_class": RuntimeClass.ADAPTIVE,
            "estimated_cost": 1.0 if not mutates_state else 2.0,
            "log_suppression": False,
            "compensation_action": "",
        }
    return ActionSpec(
        name=name,
        kind=kind,
        description=description,
        group=group,
        tags=tags,
        threat_detection_use=threat_detection_use,
        risk_level=risk_level,
        mutates_state=mutates_state,
        requires_approval=requires_approval,
        safe_retry=safe_retry,
        **authority_fields,
        **kwargs,
    )


ACTION_SPECS: tuple[ActionSpec, ...] = (
    _spec(
        "read_file", "tool", "Read a local text/PDF-adjacent file.", "read",
        ("file", "read-only", "local"),
        "Supports evidence gathering without mutation.", "low", False, False, True,
    ),
    _spec(
        "write_file", "tool", "Create or overwrite a local file.", "write",
        ("file", "mutation", "approval"),
        "Detects and gates workspace-changing file writes.", "high", True, True, False,
    ),
    _spec(
        "run_shell", "tool", "Run a shell command in the workspace.", "shell",
        ("shell", "mutation", "approval", "safe-mode"),
        "Detects shell mutation risk and preserves safe-mode enforcement.", "high", True, True, False,
    ),
    _spec(
        "web_search", "tool", "Search the web through cloud tooling.", "web",
        ("web", "network", "provider"),
        "Detects network/provider dependency before research actions.", "medium", False, False, True,
        requires_network=True, requires_provider="ollama-cloud",
    ),
    _spec(
        "query_knowledge_graph", "tool", "Query index-compute-lab ranked associations.", "read",
        ("icl", "knowledge-graph", "read-only"),
        "Detects local knowledge graph readiness for grounded context.", "low", False, False, True,
    ),
    _spec(
        "remember", "tool", "Store a durable user fact in local memory.", "memory",
        ("memory", "local", "mutation", "safe-retry"),
        "Allows the runtime to retain an explicitly stated durable fact without exposing external state.",
        "low", True, False, True,
    ),
    _spec(
        "append_lesson", "tool", "Append a durable lesson to local identity memory.", "memory",
        ("memory", "lesson", "local", "mutation", "safe-retry"),
        "Allows same-turn retention of durable corrections and workflow lessons.",
        "low", True, False, True,
    ),
    _spec(
        "model_delete", "tool", "Delete a local Ollama model.", "model",
        ("model", "destructive", "approval"),
        "Detects destructive local model actions before execution.", "high", True, True, False,
        requires_binary=("ollama",),
    ),
    _spec(
        "/safe", "slash", "Show or toggle safe mode.", "session",
        ("safety", "toggle"),
        "Surfaces shell/file safety posture.", "medium", True, True, True,
    ),
    _spec(
        "/auto", "slash", "Show or toggle auto-approval.", "session",
        ("approval", "toggle"),
        "Surfaces whether mutating actions may be auto-approved.", "high", True, True, True,
    ),
    _spec(
        "/memory-auto", "slash", "Show or toggle bounded automatic memory capture.", "memory",
        ("memory", "privacy", "toggle"),
        "Exposes the deterministic durable-memory completion gate and its persisted opt-out.",
        "medium", True, True, True,
    ),
    _spec(
        "/code-rag", "slash", "Show or toggle working-directory code retrieval.", "harness",
        ("code-rag", "privacy", "local-index", "toggle"),
        "Requires explicit versioned consent before cwd snippets can enter model prompts.",
        "medium", True, True, True,
        known_limitations=(
            "When enabled, retrieved snippets join the active provider request; /code-rag off purges persisted indexes.",
        ),
    ),
    _spec(
        "/actions", "slash", "Show available actions.", "session",
        ("registry", "read-only"),
        "Exposes safe capability discovery.", "low", False, False, True,
    ),
    _spec(
        "/doctor", "slash", "Show provider, dependency, ICL, and safety readiness.", "session",
        ("doctor", "readiness", "threat-detection"),
        "Detects missing credentials, unavailable providers, and unsafe posture.", "low", False, False, True,
    ),
    _spec(
        "/intelligence", "slash", "Run repository intelligence status/query/reindex commands.", "harness",
        ("intelligence", "project-graph", "read-only"),
        "Exposes local project graph inspection through the agent runtime.", "low", False, False, True,
        known_limitations=("The reindex subcommand persists .algo/index/project_graph.json and may require approval.",),
    ),
    _spec(
        "/intel", "slash", "Alias for /intelligence.", "harness",
        ("intelligence", "alias", "project-graph"),
        "Keeps the short repository intelligence command discoverable for agent/runtime use.", "low", False, False, True,
        replacement="/intelligence",
    ),
    _spec(
        "/intelagence", "slash", "Alias for /intelligence.", "harness",
        ("intelligence", "alias", "project-graph"),
        "Keeps the misspelled runtime command discoverable for compatibility.", "low", False, False, True,
        replacement="/intelligence",
    ),
    _spec(
        "/kernel", "slash", "Inspect promoted Algo CLI kernel specs.",
        "kernel",
        ("kernel", "registry", "read-only"),
        "Exposes productionized kernel metadata without executing intelligence workloads.", "low", False, False, True,
    ),
    _spec(
        "kernel.list", "kernel", "List promoted Algo CLI kernel specs.",
        "kernel",
        ("kernel", "registry", "read-only"),
        "Discovers promoted kernels without importing or executing workload modules.", "low", False, False, True,
    ),
    _spec(
        "kernel.show", "kernel", "Show one promoted Algo CLI kernel spec.",
        "kernel",
        ("kernel", "registry", "read-only"),
        "Inspects a named kernel contract without importing or executing workload modules.", "low", False, False, True,
    ),
    _spec(
        "agent.plan", "kernel", "Route and plan a bounded Agent Blocks run.",
        "kernel",
        ("agent", "planning", "read-only"),
        "Keeps task routing and block budgets visible before execution.", "low", False, False, True,
        known_limitations=("Invoked through /agent and /route; not a standalone model-callable tool.",),
    ),
    _spec(
        "/worktree", "slash", "Inspect and manage Algo-isolated Git worktrees.", "agent",
        ("worktree", "git", "isolation"),
        "Routes status/list safely while create/use/remove retain subcommand-specific approval gates.",
        "low", False, False, True,
        known_limitations=("Create, activate, and remove subcommands require approval when model-invoked.",),
    ),
    _spec(
        "worktree.inspect", "kernel", "Inspect registered worktree identity and Git state.", "agent",
        ("worktree", "git", "read-only"),
        "Provides bounded branch, HEAD, cleanliness, and registry evidence.", "low", False, False, True,
    ),
    _spec(
        "worktree.create", "kernel", "Create a collision-safe linked Git worktree.", "agent",
        ("worktree", "git", "mutation", "isolation"),
        "Allocates repository-hashed paths and a unique feature branch without shell interpolation.",
        "medium", True, True, False,
    ),
    _spec(
        "worktree.activate", "kernel", "Activate a verified managed worktree for the session.", "agent",
        ("worktree", "git", "session-mutation", "safe-retry"),
        "Validates repository and branch identity before changing session cwd.", "medium", True, True, True,
    ),
    _spec(
        "worktree.remove", "kernel", "Remove a clean managed worktree while retaining its branch.", "agent",
        ("worktree", "git", "destructive"),
        "Fails closed on tracked, untracked, or ignored files and never deletes the recovery branch.",
        "high", True, True, False,
    ),
    _spec(
        "/ship", "slash", "Plan or execute guarded commit, push, and pull-request phases.", "publish",
        ("git", "publish", "scrub", "pull-request"),
        "Keeps status/plan read-only and approval-gates every mutating subcommand.",
        "low", False, False, True,
        known_limitations=("Commit, push, PR, and stacked all subcommands require approval when model-invoked.",),
    ),
    _spec(
        "ship.plan", "kernel", "Fingerprint and preview structured publish readiness.", "publish",
        ("git", "publish", "read-only", "fingerprint"),
        "Binds branch, HEAD, working state, upstream, remote refs, and diff checks into a reviewable plan.",
        "low", False, False, True,
    ),
    _spec(
        "gate.pre_push", "kernel", "Verify structured outgoing-history scrub evidence before push.", "publish",
        ("git", "publish", "privacy", "gate"),
        "Rejects raw pushes unless an explicit override or valid scanner evidence is supplied.",
        "medium", False, False, True,
    ),
    _spec(
        "ship.execute", "kernel", "Run resumable commit, push, and draft-PR phases.", "publish",
        ("git", "publish", "network", "mutation"),
        "Fails closed on stale fingerprints, secret findings, remote divergence, and protected branches.",
        "high", True, True, False,
    ),
    _spec(
        "agent.delegate", "kernel", "Run a bounded 2-4 specialist agent team.",
        "kernel",
        ("agent", "delegation", "approval", "multi-agent"),
        "Gates multi-model fan-out and the integrating pipeline behind explicit approval.", "medium", True, True, False,
        known_limitations=("Child specialists are read-only; the integration pipeline is the sole mutation owner.",),
    ),
    _spec(
        "agent.report", "kernel", "Inspect agent thread status and evidence.",
        "kernel",
        ("agent", "thread", "report", "read-only"),
        "Exposes bounded parent/child evidence without rerunning work.", "low", False, False, True,
        known_limitations=("Invoked through /agent threads and /agent show.",),
    ),
    _spec(
        "agent.thread.resume", "kernel", "Resume or fork a persisted agent thread.",
        "kernel",
        ("agent", "thread", "resume", "approval"),
        "Requires approval before continuing a run that may reach mutation-capable integration blocks.", "medium", True, True, False,
        known_limitations=("Invoked through /agent resume or /agent fork; recursive delegation remains blocked.",),
    ),
    _spec(
        "small_context.ledger.write", "kernel", "Write bounded optional context to a temporary ledger.",
        "kernel",
        ("context", "small-model", "temporary", "local"),
        "Preserves full optional context for compact models while keeping the live prompt bounded.",
        "low", True, False, True,
        known_limitations=("Writes only beneath the OS temporary directory and never replaces source files.",),
    ),
    _spec(
        "small_context.ledger.preview", "kernel", "Preview small-context ledger activation.",
        "kernel",
        ("context", "small-model", "read-only"),
        "Explains whether the current model window activates external context storage.",
        "low", False, False, True,
    ),
    _spec(
        "extensions.manifest", "kernel", "Build an extension and helper readiness manifest.",
        "kernel",
        ("extensions", "manifest", "read-only"),
        "Reports plugin and helper-binary readiness without loading plugin code.",
        "low", False, False, True,
    ),
    _spec(
        "vision.screenshot_verify", "kernel", "Verify screenshot-description evidence.",
        "kernel",
        ("vision", "verification", "read-only"),
        "Turns expected and forbidden visual terms into a structured pass/fail result.",
        "low", False, False, True,
    ),
    _spec(
        "session_distribution.summarize", "kernel", "Summarize harness source concentration.",
        "kernel",
        ("harness", "telemetry", "distribution", "read-only"),
        "Flags heavy-tail concentration that can bias retrieval and evaluation results.",
        "low", False, False, True,
    ),
    _spec(
        "benchmark.compare", "kernel", "Recompute the five-axis comparative harness rating.",
        "kernel",
        ("benchmark", "competitive", "read-only", "fail-closed"),
        "Rejects arithmetic errors, ties, stale competitor evidence, and unsupported leader claims.",
        "low", False, False, True,
    ),
    _spec(
        "benchmark.report", "kernel", "Run the internal ten-gate harness readiness scorecard.",
        "kernel",
        ("benchmark", "scorecard", "read-only", "evidence"),
        "Surfaces retrieval, memory, maintenance, and benchmark regressions with structured evidence.",
        "low", False, False, True,
    ),
    _spec(
        "benchmark.algorithm_effectiveness", "kernel", "Probe production-path algorithm effectiveness.",
        "kernel",
        ("benchmark", "algorithm", "production-path", "read-only"),
        "Requires receipts from every declared retrieval/cache/admission algorithm before readiness.",
        "low", False, False, True,
    ),
    _spec(
        "repo.status", "kernel", "Inspect repository-intelligence readiness and exports.",
        "kernel",
        ("repository", "intelligence", "status", "read-only"),
        "Shows the active project root and graph capabilities without writing an index.",
        "low", False, False, True,
    ),
    _spec(
        "repo.query", "kernel", "Build an in-memory project graph and query it.",
        "kernel",
        ("repository", "intelligence", "query", "read-only"),
        "Provides symbol/file evidence for runtime navigation without persisting graph state.",
        "low", False, False, True,
    ),
    _spec(
        "repo.reindex", "kernel", "Persist the repository project graph index.",
        "kernel",
        ("repository", "intelligence", "index", "mutation", "approval"),
        "Refreshes local graph state and keeps model-triggered writes approval-gated.",
        "medium", True, True, True,
    ),
    _spec(
        "harness.fusion.lexical_rank", "kernel", "Rank harness records with BM25 plus curated field boosts.",
        "kernel",
        ("harness", "retrieval", "bm25", "lexical", "read-only"),
        "Weights rare exact terms without discarding title, path, or canonical catalog priorities.",
        "low", False, False, True,
    ),
    _spec(
        "harness.fusion.rank", "kernel", "Fuse BM25 and exact-vector rankings with Reciprocal Rank Fusion.",
        "kernel",
        ("harness", "retrieval", "rrf", "fusion", "read-only"),
        "Uses both exact lexical and semantic evidence for automatic prompt context and slash search.",
        "low", False, False, True,
    ),
    _spec(
        "harness.fusion.explain", "kernel", "Expose lexical, vector, and RRF rank provenance.",
        "kernel",
        ("harness", "retrieval", "provenance", "telemetry", "read-only"),
        "Makes every fused result attributable to its contributing rankers and component scores.",
        "low", False, False, True,
    ),
    _spec(
        "harness.index.refresh_changed", "kernel", "Refresh changed harness records and content-addressed code chunks.",
        "kernel",
        ("harness", "index", "incremental", "content-hash", "mutation", "approval"),
        "Reuses unchanged records and content-identical chunk embeddings before rebuilding sidecars.",
        "medium", True, True, True,
    ),
    _spec(
        "harness.index.status", "kernel", "Inspect incremental index reuse and embedding readiness.",
        "kernel",
        ("harness", "index", "incremental", "status", "read-only"),
        "Surfaces record reuse, content-addressed embedding reuse, and rebuild work without mutation.",
        "low", False, False, True,
    ),
    _spec(
        "chain.evaluate", "kernel", "Evaluate the ordered runtime tool policy chain.",
        "kernel",
        ("policy", "chain", "enforcement", "read-only"),
        "Fail-closes unknown tools, invalid capability tiers, and safe-mode shell mutations.",
        "medium", False, False, True,
    ),
    _spec(
        "chain.audit", "kernel", "Record runtime policy-chain decisions in telemetry.",
        "kernel",
        ("policy", "audit", "telemetry", "read-only"),
        "Preserves tier, capability mask, and fired-rule evidence for every tool preflight.",
        "low", False, False, True,
    ),
    _spec(
        "capability.mask", "kernel", "Compute a stable capability mask for a runtime tool.",
        "kernel",
        ("policy", "capability", "mask", "read-only"),
        "Maps tool requirements to stable read/write/shell/network/model capability bits.",
        "low", False, False, True,
    ),
    _spec(
        "capability.tier", "kernel", "Assign the least-privileged runtime capability tier.",
        "kernel",
        ("policy", "capability", "tier", "least-privilege"),
        "Ensures each tool's capability mask fits its structural permission tier.",
        "low", False, False, True,
    ),
    _spec(
        "runtime.qos.classify", "kernel", "Classify every model tool dispatch by runtime posture.",
        "kernel",
        ("runtime", "qos", "dispatch", "telemetry"),
        "Labels tool calls adaptive, interactive, or background before execution.",
        "low", False, False, True,
    ),
    _spec(
        "runtime.qos.schedule", "kernel", "Order bounded tool batches by QoS class weight and estimated cost.",
        "kernel",
        ("runtime", "qos", "scheduler", "bounded-batch"),
        "Provides deterministic submission order; the queue primitive also exposes aging for persistent callers.",
        "low", False, False, True,
        known_limitations=(
            "Typical batches that fit the worker pool start together; persistent cross-batch aging is not wired.",
        ),
    ),
    _spec(
        "runtime.log_path", "kernel", "Attach a stable named log destination to tool telemetry.",
        "kernel",
        ("runtime", "qos", "logs", "sensitive-data"),
        "Provides per-tool log destinations while suppressing credential-bearing tool logs.",
        "low", False, False, True,
        known_limitations=("The runtime records the destination as metadata; subprocess redirection remains tool-specific.",),
    ),
    _spec(
        "tool_sequence.score", "kernel", "Score recent runtime tool cadence from the bounded attempt ledger.",
        "kernel",
        ("runtime", "quality", "verification", "read-only"),
        "Surfaces verification-after-edit and test-repair cadence without inspecting private reasoning.",
        "low", False, False, True,
        known_limitations=("Private model reasoning is deliberately not persisted or scored by /selfcheck.",),
    ),
    _spec(
        "cache.tinylfu.admit", "kernel", "Protect hot embedding-cache entries from one-off scan pollution.",
        "kernel",
        ("cache", "memory", "tinylfu", "bounded", "admission"),
        "Uses recency plus bounded frequency evidence before evicting reusable query vectors.",
        "low", False, False, True,
    ),
    _spec(
        "cache.tinylfu.stats", "kernel", "Expose bounded cache admission and hit-rate telemetry.",
        "kernel",
        ("cache", "memory", "telemetry", "read-only"),
        "Makes cache hit, rejection, eviction, and sketch-decay behavior measurable.",
        "low", False, False, True,
    ),
    _spec(
        "performance.cusum.detect", "kernel", "Detect sustained runtime latency shifts with robust CUSUM.",
        "kernel",
        ("performance", "telemetry", "regression", "cusum", "read-only"),
        "Separates sustained regressions from isolated latency spikes using comparable event series.",
        "low", False, False, True,
    ),
    _spec(
        "performance.cusum.selfcheck", "kernel", "Surface latency trend evidence in /selfcheck.",
        "kernel",
        ("performance", "selfcheck", "diagnostics", "read-only"),
        "Reports stable, improving, regressing, or insufficient-data state without model inference.",
        "low", False, False, True,
    ),
    _spec(
        "ollama-cli-env", "legacy", "Legacy OLLAMA_CLI_* environment variables.", "legacy",
        ("legacy", "env", "deprecated"),
        "Detects stale legacy configuration that may confuse model/provider routing.", "medium", False, False, True,
        archived=True,
        archived_reason="Algo CLI rebrand replaced OLLAMA_CLI_* names with ALGO_CLI_* names.",
        replacement="Use ALGO_CLI_* environment variables and the algo-cli command.",
    ),
    _spec(
        "google_workspace.read", "provider", "Google Workspace access (read/write Drive/Docs/Sheets/Calendar plus Gmail read/draft creation) via OAuth.",
        "provider",
        ("google-workspace", "oauth", "read-write", "network"),
        "Provides Google Workspace access behind the `algo-cli config setup google` OAuth flow; Gmail writes are limited to draft creation for user review.",
        "medium", False, False, True,
        requires_network=True,
        requires_provider="google-workspace",
        known_limitations=(
            "Drive/Docs/Sheets/Calendar OAuth scopes are full read/write; CLI subcommands may expose write operations incrementally.",
            "Gmail compose scope is used only for draft creation; direct send is not exposed.",
            "Tokens stored in CONFIG_DIR/google_workspace_auth.json (POSIX 0600).",
        ),
    ),
    _spec(
        "/google-login", "slash", "Authenticate with Google Workspace (OAuth2 + PKCE).",
        "provider",
        ("google-workspace", "oauth", "auth"),
        "Compatibility route for existing sessions; new setup lives under `algo-cli config`.", "medium", True, True, True,
        archived=True,
        archived_reason="Provider setup moved out of the normal slash palette.",
        replacement="Use `algo-cli config auth google login`.",
    ),
    _spec(
        "/google-logout", "slash", "Revoke Google Workspace tokens locally.",
        "provider",
        ("google-workspace", "auth", "logout"),
        "Compatibility route for existing sessions; new setup lives under `algo-cli config`.", "medium", True, True, True,
        archived=True,
        archived_reason="Provider setup moved out of the normal slash palette.",
        replacement="Use `algo-cli config auth google logout`.",
    ),
    _spec(
        "/google-status", "slash", "Show Google Workspace auth state.",
        "provider",
        ("google-workspace", "auth", "status"),
        "Compatibility route for existing sessions; new setup lives under `algo-cli config`.", "low", False, False, True,
        archived=True,
        archived_reason="Provider setup moved out of the normal slash palette.",
        replacement="Use `algo-cli config auth google status`.",
    ),
    _spec(
        "/config", "slash", "Configure or inspect connected providers outside the normal slash palette.",
        "provider",
        ("provider", "configuration", "credentials", "oauth"),
        "`/config status` is read-only; setup and login paths can write local credential state or open a browser.",
        "medium", True, True, False,
    ),
    _spec(
        "/google", "slash", "Run Google Workspace commands (Drive/Docs/Sheets/Calendar read/write plus Gmail read/drafts).",
        "provider",
        ("google-workspace", "command", "read-write", "gmail-drafts"),
        "Dispatches to Google Workspace operations; Gmail direct send is not exposed.", "medium", False, False, True,
    ),
    _spec(
        "/plugins", "slash", "Show validated manifest-only plugin metadata.",
        "plugins",
        ("plugins", "discovery", "manifest-only", "read-only"),
        "Lists strict manifests and rejections without importing plugin code.", "low", False, False, True,
    ),
    _spec(
        "/credentials", "slash", "List credential helpers or check a named helper key with values redacted.",
        "credentials",
        ("credentials", "auth", "read-only"),
        "Surfaces registered credential helper backends and their status.", "low", False, False, True,
    ),
    _spec(
        "/url-scheme", "slash", "Parse an algo-cli:// deep link.",
        "url-scheme",
        ("url-scheme", "deep-link", "read-only"),
        "Parses and validates algo-cli:// URLs for deep-linking from other tools.", "low", False, False, True,
    ),
    _spec(
        "plugins_discover", "tool", "Discover plugins from ~/.algo_cli/plugins/ directory.",
        "plugins",
        ("plugins", "discovery", "read-only"),
        "Discovers plugin manifests from the plugins directory.", "low", False, False, True,
    ),
    _spec(
        "plugins_load", "tool", "Report that in-process plugin code loading is blocked.",
        "plugins",
        ("plugins", "loading", "blocked", "handoff"),
        "Fails closed because importing Python is arbitrary in-process code execution.", "high", True, True, False,
        known_limitations=("Callable plugin actions, commands, and tools are prohibited; no local plugin execution route is enabled.",),
        archived=True,
        archived_reason="In-process Python plugins are not a security boundary and remain disabled.",
        replacement="No replacement is enabled during the hardening freeze.",
    ),
    _spec(
        "version_manifest_build", "tool", "Build a version manifest with CLI, Python, platform, harness, and plugin versions.",
        "version",
        ("version", "manifest", "read-only"),
        "Assembles full system version state for debugging and reporting.", "low", False, False, True,
    ),
    _spec(
        "extensions_manifest_build", "tool", "Build an extension manifest with plugin/helper binary versions and status.",
        "version",
        ("version", "manifest", "extensions", "read-only"),
        "Assembles plugin/helper component state as a sibling to version_manifest_build.", "low", False, False, True,
    ),
    _spec(
        "runtime_qos_hint", "tool", "Classify a tool call's runtime QoS and named log destination.",
        "runtime",
        ("runtime", "qos", "logs", "read-only"),
        "Applies launchd-style POSIXSpawnType and StandardErrorPath patterns to tool calls.", "low", False, False, True,
    ),
    _spec(
        "screenshot_description_verify", "tool", "Verify a screenshot description against expected and forbidden terms.",
        "vision",
        ("vision", "verification", "screenshot", "read-only"),
        "Turns browser/vision screenshot descriptions into structured pass/fail evidence.", "low", False, False, True,
    ),
    _spec(
        "capability_mask_describe", "tool", "Describe a stable capability bit mask from a tier and/or capability names.",
        "policy",
        ("policy", "capability", "bit-mask", "read-only"),
        "Provides Apple audit_class-style stable numeric capability masks for tools and kernels.", "low", False, False, True,
    ),
    _spec(
        "small_context_ledger_preview", "tool", "Preview the small-context ledger activation decision for a model/window.",
        "context",
        ("context", "small-model", "ledger", "read-only"),
        "Shows whether a <75k context model will use the temp context-ledger refresh path.", "low", False, False, True,
    ),
    _spec(
        "credential_helpers_get", "tool", "Check a named helper for credential presence without exposing plaintext.",
        "credentials",
        ("credentials", "auth", "read-only"),
        "Retrieves secrets through pluggable credential backends.", "low", False, False, True,
    ),
    _spec(
        "credential_helpers_store", "tool", "Store a credential by named helper and key.",
        "credentials",
        ("credentials", "auth", "mutation"),
        "Stores secrets through pluggable credential backends.", "medium", True, True, False,
    ),
    _spec(
        "url_scheme_parse", "tool", "Parse an algo-cli:// deep link into an action descriptor.",
        "url-scheme",
        ("url-scheme", "deep-link", "read-only"),
        "Parses algo-cli:// URLs into structured action descriptors.", "low", False, False, True,
    ),
    _spec(
        "action_search", "tool", "Discover deferred registered actions and their exact schemas.",
        "program",
        ("action", "discovery", "deferred-schema", "read-only", "bm25"),
        "Searches the capability catalog without granting or executing any action.",
        "low", False, False, True,
    ),
    _spec(
        "action_program", "tool", "Compile and execute a bounded typed action plan.",
        "program",
        ("action", "typed-plan", "bounded", "artifact", "approval", "orchestrator"),
        "Orchestrates only runtime-authorized actions; every nested effect retains its own policy and approval.",
        "high", True, False, False,
        known_limitations=(
            "The orchestrator itself is not blanket-approved; nested mutation and external actions keep per-action approval.",
            "Session commands, plugin loading, recursive programs, ambient code execution, and capability expansion are forbidden.",
        ),
    ),
)


def list_action_specs(*, include_archived: bool = False) -> tuple[ActionSpec, ...]:
    if include_archived:
        return ACTION_SPECS
    return tuple(spec for spec in ACTION_SPECS if not spec.archived)


def _first_doc_line(obj: Any, fallback: str) -> str:
    doc = inspect.getdoc(obj) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first or fallback


def _generated_tool_spec(name: str, fn: Any) -> ActionSpec:
    policy = policy_for_action(name)
    network = name.startswith("web_") or name.startswith("x_") or name in {
        "model_pull",
        "model_create",
    }
    provider = (
        "ollama-cloud" if name.startswith("web_")
        else "xai" if name == "x_search"
        else "x-account" if name.startswith("x_account_")
        else None
    )
    return _spec(
        name,
        "tool",
        _first_doc_line(fn, f"Runtime callable tool: {name}."),
        "runtime",
        (("curated-runtime", "runtime", "tool") if policy.curated else ("unclassified", "runtime", "tool")),
        (
            "Curated runtime policy covers this callable even though it has no long-form registry entry."
            if policy.curated
            else "Unclassified runtime callable is denied until an explicit authority policy is added."
        ),
        "high",
        True,
        True,
        False,
        requires_network=network,
        requires_provider=provider,
    )


def _generated_slash_spec(command: str, description: str) -> ActionSpec:
    from .nathan_runtime import session_command_requires_approval

    requires_approval = session_command_requires_approval(command)
    return _spec(
        command,
        "slash",
        description or f"Runtime slash command: {command}.",
        "session",
        ("generated", "runtime", "slash"),
        "Generated slash-command coverage spec; explicit ActionSpec can override risk metadata.",
        "medium" if requires_approval else "low",
        requires_approval,
        requires_approval,
        not requires_approval,
    )


def effective_action_specs(*, include_archived: bool = False) -> tuple[ActionSpec, ...]:
    """Explicit ActionSpecs plus generated coverage specs for runtime tools/slash commands."""
    from .oliver_slash_dispatch import SLASH_COMMANDS
    from .tools import TOOL_MAP

    specs = list(list_action_specs(include_archived=include_archived))
    covered_tools = {spec.name for spec in specs if spec.kind == "tool"}
    covered_slashes = {spec.name for spec in specs if spec.kind == "slash"}
    for name, fn in sorted(TOOL_MAP.items()):
        if name not in covered_tools:
            specs.append(_generated_tool_spec(name, fn))
    for command, description in SLASH_COMMANDS:
        if command not in covered_slashes:
            specs.append(_generated_slash_spec(command, description))
            covered_slashes.add(command)
    return tuple(specs)


def get_action_spec(name: str) -> ActionSpec:
    normalized = name.strip().lower()
    for spec in effective_action_specs(include_archived=True):
        if spec.name.lower() == normalized:
            return spec
    raise KeyError(f"Unknown action spec: {name}")


def action_requires_approval(name: str) -> bool:
    """Return the fail-closed confirmation requirement for a tool."""

    return policy_for_action(name).requires_approval


def action_confirmation_mode(name: str) -> ConfirmationMode:
    """Return the strongest static confirmation class for a tool."""

    return policy_for_action(name).confirmation_mode


def action_capability_mask(name: str) -> int:
    """Return the curated capability mask, or UNCLASSIFIED for an unknown tool."""

    return policy_for_action(name).capability_mask.value


def _declared_dispatch_commands() -> set[str]:
    """Extract literal top-level command branches from handle_command for diagnostics."""

    from .oliver_slash_dispatch import handle_command

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handle_command)))
    except (OSError, TypeError, IndentationError, SyntaxError):
        return set()
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "command":
            continue
        for operator, comparator in zip(node.ops, node.comparators):
            if isinstance(operator, ast.Eq) and isinstance(comparator, ast.Constant):
                if isinstance(comparator.value, str):
                    commands.add(comparator.value)
            elif isinstance(operator, ast.In) and isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                commands.update(
                    item.value
                    for item in comparator.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
    return commands


def audit_action_registry_runtime() -> DoctorReport:
    """Check that declared tool/slash actions resolve to runnable runtime entries."""
    from .oliver_slash_dispatch import SLASH_COMMANDS, SLASH_COMMAND_ALIASES
    from .tools import TOOL_MAP

    tool_names = set(TOOL_MAP)
    slash_list = [command for command, _description in SLASH_COMMANDS]
    slash_names = set(slash_list)
    registered_tool_specs = [spec for spec in list_action_specs() if spec.kind == "tool"]
    registered_slash_specs = [spec for spec in list_action_specs() if spec.kind == "slash"]
    covered_specs = effective_action_specs()
    covered_tool_specs = [spec for spec in covered_specs if spec.kind == "tool"]
    covered_slash_specs = [spec for spec in covered_specs if spec.kind == "slash"]
    curated_runtime_tool_specs = [spec for spec in covered_tool_specs if "curated-runtime" in spec.tags]
    unclassified_tool_specs = [spec for spec in covered_tool_specs if not spec.curated]
    generated_slash_specs = [spec for spec in covered_slash_specs if "generated" in spec.tags]
    missing_tools: list[str] = []
    missing_slashes: list[str] = []
    non_callable_tools: list[str] = []
    non_callable_runtime_tools = sorted(name for name, fn in TOOL_MAP.items() if not callable(fn))
    duplicate_slashes = sorted({command for command in slash_list if slash_list.count(command) > 1})
    slash_roots = {command.split()[0] for command in slash_list}
    dispatch_commands = _declared_dispatch_commands()
    undispatched_slashes = sorted(
        root
        for root in slash_roots
        if root not in dispatch_commands and root not in SLASH_COMMAND_ALIASES
    )
    broken_aliases = sorted(
        source
        for source, target in SLASH_COMMAND_ALIASES.items()
        if source not in slash_roots or target not in dispatch_commands
    )

    for spec in registered_tool_specs + registered_slash_specs:
        if spec.kind == "tool":
            if spec.name not in tool_names:
                missing_tools.append(spec.name)
            elif not callable(TOOL_MAP.get(spec.name)):
                non_callable_tools.append(spec.name)
        elif spec.kind == "slash" and spec.name not in slash_names:
            missing_slashes.append(spec.name)

    findings: list[DoctorFinding] = []
    if missing_tools:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"registered tool specs missing from TOOL_MAP: {', '.join(sorted(missing_tools))}",
            "Add the tool to TOOL_MAP/ALL_TOOLS or remove/archive the ActionSpec.",
        ))
    if non_callable_runtime_tools:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"runtime TOOL_MAP entries are not callable: {', '.join(non_callable_runtime_tools)}",
            "Ensure every TOOL_MAP value is a callable function.",
        ))
    if non_callable_tools:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"registered tool specs are not callable: {', '.join(sorted(non_callable_tools))}",
            "Ensure the TOOL_MAP entry is a callable function.",
        ))
    if missing_slashes:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"registered slash specs missing from SLASH_COMMANDS: {', '.join(sorted(missing_slashes))}",
            "Add the command to SLASH_COMMANDS/handle_command or remove/archive the ActionSpec.",
        ))
    if duplicate_slashes:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"duplicate slash commands declared: {', '.join(duplicate_slashes)}",
            "Deduplicate SLASH_COMMANDS so completion/help/dispatch remain deterministic.",
        ))
    if undispatched_slashes:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"slash commands declared but not dispatched: {', '.join(undispatched_slashes)}",
            "Add a handle_command branch or remove the stale command from SLASH_COMMANDS.",
        ))
    if broken_aliases:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            f"slash aliases have missing sources or dispatch targets: {', '.join(broken_aliases)}",
            "Register each alias source and point it at a dispatched canonical command.",
        ))
    if unclassified_tool_specs:
        findings.append(DoctorFinding(
            "blocked",
            "action-registry",
            "runtime tools lack curated fail-closed authority policy: "
            + ", ".join(sorted(spec.name for spec in unclassified_tool_specs)),
            "Add each tool to CURATED_TOOL_POLICIES before making it runnable.",
        ))

    if not findings:
        findings.append(DoctorFinding(
            "ready",
            "action-registry",
            f"runtime surface: {len(tool_names)} tools callable, {len(slash_names)} slash commands declared",
        ))
        findings.append(DoctorFinding(
            "ready",
            "action-registry",
            f"ActionSpec coverage: {len(covered_tool_specs)}/{len(tool_names)} tools covered "
            f"({len(registered_tool_specs)} explicit, {len(curated_runtime_tool_specs)} curated-runtime), "
            f"{len(covered_slash_specs)}/{len(slash_names)} slash commands covered "
            f"({len(registered_slash_specs)} explicit, {len(generated_slash_specs)} generated)",
            "Every runtime tool policy is curated; slash coverage remains diagnostic until its hardening milestone.",
        ))
        findings.append(DoctorFinding(
            "ready",
            "action-registry",
            f"{len(registered_tool_specs)} explicit tool specs and {len(registered_slash_specs)} explicit slash specs resolve",
        ))

    overall: FindingStatus = "blocked" if any(f.status == "blocked" for f in findings) else "ready"
    return DoctorReport(overall, tuple(findings))


def render_action_registry_runtime_audit(report: DoctorReport) -> str:
    lines = [f"Action registry runtime audit: {report.overall_status.upper()}"]
    for finding in report.findings:
        lines.append(f"- {finding.status.upper():8} {finding.area}: {finding.message}")
        if finding.recommendation:
            lines.append(f"           recommendation: {finding.recommendation}")
    return "\n".join(lines)


def _check_ollama_host(host: str) -> bool:
    try:
        with urlopen(f"{host.rstrip('/')}/api/tags", timeout=0.5) as response:  # nosec: local readiness probe
            return 200 <= int(response.status) < 300
    except (OSError, URLError, ValueError):
        return False


def build_doctor_report(cfg: Any) -> DoctorReport:
    findings: list[DoctorFinding] = []

    if shutil.which("ollama"):
        findings.append(DoctorFinding("ready", "ollama", "ollama binary found"))
    else:
        findings.append(DoctorFinding(
            "degraded", "ollama", "ollama binary not found on PATH",
            "Install Ollama or ensure ollama is on PATH for local model operations.",
        ))

    cloud = bool(getattr(cfg, "cloud", False))
    host = str(getattr(cfg, "host", "http://localhost:11434"))
    load_runtime_env(override=True)
    has_ollama_api_key = bool(os.environ.get("OLLAMA_API_KEY", "").strip())
    if cloud:
        if has_ollama_api_key:
            findings.append(DoctorFinding("ready", "ollama-cloud", "OLLAMA_API_KEY present"))
        else:
            findings.append(DoctorFinding(
                "degraded", "ollama-cloud", "direct Cloud API disabled because OLLAMA_API_KEY is missing",
                "Signed-in local Ollama can still run :cloud models; set OLLAMA_API_KEY only for direct API/web tools.",
            ))
    else:
        if _check_ollama_host(host):
            findings.append(DoctorFinding("ready", "ollama-host", f"local Ollama host reachable: {host}"))
        else:
            findings.append(DoctorFinding(
                "degraded", "ollama-host", f"local Ollama host not reachable: {host}",
                "Start Ollama, run /login for local :cloud models, or set OLLAMA_API_KEY for direct API/web tools.",
            ))

    if has_ollama_api_key:
        findings.append(DoctorFinding(
            "ready",
            "web-tools",
            "web_search/web_fetch configured via OLLAMA_API_KEY",
        ))
    else:
        findings.append(DoctorFinding(
            "degraded",
            "web-tools",
            "web_search/web_fetch disabled because OLLAMA_API_KEY is missing",
            "Set ALGO_CLI_ENV_FILE or ~/.algo_cli/env with OLLAMA_API_KEY, then rerun /doctor.",
        ))

    # xAI API access is optional.  A missing key must not make a fresh local
    # install look unhealthy, but the readiness report must not claim that an
    # undocumented consumer OAuth lane is usable.
    try:
        from . import xai_auth

        xai_status = xai_auth.auth_status()
        if not xai_status.get("api_key_configured"):
            findings.append(DoctorFinding(
                "ready",
                "xai-api",
                "optional xAI API key is not configured",
                "Run `algo-cli config setup xai` only if you want to enable Grok API models or x_search.",
            ))
        else:
            findings.append(DoctorFinding(
                "ready",
                "xai-api",
                "optional xAI API key configured (value redacted)",
            ))
        if xai_status.get("legacy_oauth_detected"):
            findings.append(DoctorFinding(
                "degraded",
                "xai-legacy-oauth",
                "obsolete xAI OAuth state detected and ignored",
                "Remove it with `algo-cli config auth xai logout` or reconfigure with `algo-cli config setup xai`.",
            ))
    except Exception as exc:  # pragma: no cover - best-effort diagnostic
        findings.append(DoctorFinding(
            "degraded",
            "xai-api",
            f"xai_auth import failed: {exc}",
            "Reinstall Algo CLI; the module is required for optional xAI API commands.",
        ))

    from . import index_compute_lab

    root = index_compute_lab.resolve_lab_root()
    icl_enabled = bool(getattr(cfg, "index_compute_lab_auto_inject", False))
    if not icl_enabled:
        findings.append(DoctorFinding(
            "ready", "index-compute-lab", "optional index-compute-lab context is disabled",
            "Use /icl on only when this local source is appropriate for the selected model provider.",
        ))
    elif not root.exists():
        findings.append(DoctorFinding(
            "degraded", "index-compute-lab", f"enabled index-compute-lab root is missing: {root}",
            "Set ALGO_CLI_INDEX_COMPUTE_LAB_ROOT or clone/build the lab.",
        ))
    elif index_compute_lab.lab_available():
        findings.append(DoctorFinding("ready", "index-compute-lab", "index-compute-lab graph ready"))
    else:
        findings.append(DoctorFinding(
            "degraded", "index-compute-lab", "index-compute-lab root exists but query assets are missing",
            "Need query.py, atoms/ranked-association-map.json, and atoms/alias-table.json.",
        ))

    if bool(getattr(cfg, "safe_mode", True)):
        findings.append(DoctorFinding("ready", "safety", "safe mode enabled"))
    else:
        findings.append(DoctorFinding(
            "degraded", "safety", "safe mode disabled",
            "Use /safe on before high-risk shell/file work.",
        ))

    if bool(getattr(cfg, "auto_approve_active", getattr(cfg, "auto_mode", False))):
        findings.append(DoctorFinding(
            "degraded", "approval", "auto-approval enabled",
            "Use /auto off when reviewing risky mutation paths.",
        ))
    else:
        findings.append(DoctorFinding("ready", "approval", "manual approval required for dangerous actions"))

    legacy_env = sorted(k for k in os.environ if k.startswith("OLLAMA_CLI_"))
    if legacy_env:
        findings.append(DoctorFinding(
            "degraded", "legacy", f"legacy OLLAMA_CLI_* env vars present: {', '.join(legacy_env)}",
            "Rename to ALGO_CLI_* equivalents.",
        ))

    # Google Workspace readiness
    try:
        from . import google_workspace_auth
        status = google_workspace_auth.auth_status()
        if not status.get("client_configured"):
            findings.append(DoctorFinding(
                "degraded", "google-workspace",
                "GOOGLE_OAUTH_CLIENT_ID not set",
                "Create a Desktop app OAuth client, then run `algo-cli config setup google` before logging in.",
            ))
        elif status.get("authenticated"):
            findings.append(DoctorFinding(
                "ready", "google-workspace",
                f"Google Workspace authenticated (expires in {int(status.get('expires_in', 0))}s)",
            ))
        else:
            findings.append(DoctorFinding(
                "degraded", "google-workspace",
                "Google OAuth client configured but no active session",
                "Run `algo-cli config auth google login` to start the loopback flow.",
            ))
    except Exception as exc:  # pragma: no cover - best-effort diagnostic
        findings.append(DoctorFinding(
            "degraded", "google-workspace", f"google_workspace_auth import failed: {exc}",
            "Reinstall Algo CLI; the module is required for /google-* commands.",
        ))

    if any(f.status == "blocked" for f in findings):
        overall: FindingStatus = "blocked"
    elif any(f.status == "degraded" for f in findings):
        overall = "degraded"
    else:
        overall = "ready"
    return DoctorReport(overall, tuple(findings))


def render_doctor(report: DoctorReport) -> str:
    lines = [f"Algo CLI doctor: {report.overall_status.upper()}"]
    for finding in report.findings:
        lines.append(f"- {finding.status.upper():8} {finding.area}: {finding.message}")
        if finding.recommendation:
            lines.append(f"           recommendation: {finding.recommendation}")
    return "\n".join(lines)


def render_action_registry(*, include_archived: bool = False) -> str:
    lines = ["Algo CLI ActionSpec registry:"]
    for spec in list_action_specs(include_archived=include_archived):
        status = "ARCHIVED" if spec.archived else "ACTIVE"
        mutation = "mutates" if spec.mutates_state else "read-only"
        approval = "approval" if spec.requires_approval else "no-approval"
        lines.append(f"- {spec.name} [{status}] ({spec.kind}/{spec.group}, {spec.risk_level}, {mutation}, {approval})")
        lines.append(f"  {spec.description}")
        if spec.replacement:
            lines.append(f"  replacement: {spec.replacement}")
    return "\n".join(lines)
