"""Repository intelligence data structures and index builders."""

from .project_graph import (
    FileNode,
    ImportEdge,
    ProjectGraph,
    SymbolNode,
    TestMapping,
    build_project_graph,
    query_project_graph,
)
from .log2_histogram import Log2Histogram, LatencyTimer, record_latency, get_histogram
from .incremental_index import IncrementalIndex, ChangeType, ChangeRecord, FileWatermark
from .gatherer import GathererQueue, GathererEntry, TransactionFlags
from .prefetch import PredictivePrefetch, AccessRecord
from .bloom_filter import BloomFilter, CountingBloomFilter
from .count_min import CountMinSketch, HeavyHitters
from .rate_limiter import TokenBucket, SlidingWindowCounter
from .consistent_hash import ConsistentHashRing
from .minhash_lsh import MinHasher, LSHIndex
from .hyperloglog import HyperLogLog
from .document_ingest import (
    ConverterRegistry,
    ConversionResult,
    Converter,
    CSVConverter,
    JSONConverter,
    XMLConverter,
    HTMLConverter,
    TextConverter,
    ZipConverter,
    UnsupportedFormat,
    default_registry,
)
from .flow_dag import (
    FlowDefinition,
    FlowNode,
    FlowEval,
    FlowResult,
    FlowTrace,
    FlowExecutor,
    FlowError,
    parse_flow,
    detect_cycles,
    topological_sort,
)
from .agent_runtime import (
    Message,
    TraceEvent,
    AgentRuntime,
    Agent,
    SimpleAgent,
    ToolAction,
    ToolResult,
    Workbench,
    AgentAsTool,
)
from .graph_rag import (
    Entity,
    Relationship,
    Community,
    GraphRAGIndex,
    extract_entities,
    extract_relationships,
    detect_communities,
    build_community_summaries,
    local_query,
    global_query,
    build_index,
)
from .extension_host import (
    ExtensionManifest,
    CommandContribution,
    ToolContribution,
    ConverterContribution,
    MaintenanceCheckContribution,
    ExtensionHost,
    matches_activation,
)
from .iteration_plan import (
    IterationPlan,
    EndgameChecklist,
    ChecklistItem,
    CheckState,
    IterationPlanManager,
    default_endgame_checklist,
)
from .utility_registry import (
    Utility,
    UtilityRegistry,
    default_registry as default_utility_registry,
)
from .shell_session import (
    ShellSession,
    CommandRecord,
    OutputFrame,
    strip_ansi,
    normalize_vt,
    has_ansi,
    check_shell_safety,
)
from .source_registry import (
    Source,
    SourceRegistry,
    SourceRecord,
    SourceType,
)
from .actionability import (
    ActionabilityCheck,
    ActionabilityReport,
    ActionTrace,
    TraceRecorder,
    auto_wait,
    check_exists,
    check_visible,
    check_stable,
    check_enabled,
    check_receives_input,
)
from .changelog import (
    ChangeFile,
    ChangeKind as ChangelogChangeKind,
    ReleaseNotes,
    ChangelogManager,
    write_change_file,
)
from .kernel_plugins import (
    Kernel,
    ToolSchema,
    ToolParam,
    kernel_function,
    introspect_tool,
    validate_structured_output,
)
from .process_framework import (
    Process,
    ProcessStep,
    ProcessState,
    make_bid_process,
    make_permit_process,
)
from .agent_benchmark import (
    AgentBenchmark,
    AgentEnv,
    BenchmarkScenario,
    EpisodeResult,
    make_find_file_scenario,
    make_pdf_extract_scenario,
)
from .group_chat import (
    ChatAgent,
    GroupChatOrchestration,
    GroupChatResult,
    AgentMessage,
    make_writer_reviewer,
    make_extractor_verifier,
    make_planner_executer,
)
# B49-B60: GitHub repo research
from .query_expansion import QueryExpander, QueryVariant, SearchResult, RerankedResult
from .hash_dedup import HashDeduplicator, FileRecord, HashChange, ChangeKind
from .permission_modes import PermissionMode, PermissionManager, PermissionLevel
from .session_fork import SessionFork, SessionStore
from .dag_orchestration import DAGNode, DAGEdge, DependencyGraph
from .boundary_compaction import BoundaryAwareCompactor, CompactionResult
from .context_ops import TokenBudgetCompiler, ContextItem, ContextBOM
from .memory_evolution import MemorySkill, MemorySkillEvolver
from .extension_manifest import ExtensionCatalog, CatalogSource, ExtensionContribution
from .agent_arena import AgentArena, ArenaEntry, ArenaResult
from .daemon_mode import DaemonAgent, ClientSession, SharedMessage
from .lsp_integration import LSPManager, LSPManager as LSPClient, LSPServer
# B61-B68: Web scraping + deep research
from .content_extractor import ContentExtractor, ExtractedContent, URLDeduplicator
from .deep_research import DeepResearchEngine, DeepResearchEngine as DeepResearcher, QueryDecomposer, GapFiller
from .evidence_graph import EvidenceGraph, Source as EvidenceSource, Claim
from .critic_loop import CriticLoop, BudgetGuard, CriticResult, CriticDimension
from .parallel_fanout import ParallelFanOut, ParallelFanOut as ParallelFanout, FanOutTask, FanOutResult
from .cross_source import CrossSourceAnalyzer, CrossSourceAnalysis
from .clarification_gate import ClarificationGate, Clarification, ClarificationResult
from .research_workspace import ResearchWorkspace, ResearchBranch, ResearchArtifact
# B69-B76: Subagent/threading
from .subagent_spawner import SubAgentSpawner, SubAgentConfig, SubAgentResult
from .parallel_delegation import ParallelDelegator, DelegatedTask, DelegationResult
from .cow_state import COWStateManager, COWState
from .team_execution import TeamExecutor, TeamMember, TeamResult, TeamStrategy
from .agents_as_tools import AgentsAsTools, AgentTool, HandoffResult
from .saga_pattern import SagaOrchestrator, SagaOrchestrator as Saga, SagaStep, SagaResult
from .cavecrew import Cavecrew, CrewMember, CrewTask, CrewRole
from .spawn_scales import SpawnScaleSelector, SpawnScale, ScaleConfig
# B77-B86: Coding patterns
from .code_graph import CodeGraph, CodeNode, CodeEdge
from .structural_validator import StructuralValidator, Violation, CircuitBreaker
from .shadow_editor import ShadowEditor, EditValidation, LSPDiagnostic, LSPDiagnostic as ShadowLSPDiagnostic
from .occ_editor import OCCEditor, WriteResult
from .ralph_loop import RalphLoop, TestRun, RalphResult
from .golden_master import GoldenMaster, GoldenMasterRunner
from .refactor_transaction import RefactorTransaction, Savepoint, TransactionResult
from .task_classifier import TaskClassifier, AgentDelegator, TaskClassification, TaskComplexity, TaskDomain
from .coderank import CodeRank, CodeRankResult
from .repo_map import RepoMapEntry, rank_repo_map, render_repo_map, snapshot_project_graph
from .backpressure import BackpressureMonitor, BackpressureMonitor as BackpressureController, BackpressureSignal, PressureLevel
# Autonomous Engineer kernel (optimization engine)
from .autonomous_engineer import (
    Config as AEConfig,
    MemoryEngine,
    ExecutionSandbox,
    PerformanceWorker,
    LLMRouter,
    Scheduler as AEScheduler,
    OptimizeSpec,
)

__all__ = [
    # project_graph
    "FileNode",
    "ImportEdge",
    "ProjectGraph",
    "SymbolNode",
    "TestMapping",
    "build_project_graph",
    "query_project_graph",
    # log2_histogram (B29)
    "Log2Histogram",
    "LatencyTimer",
    "record_latency",
    "get_histogram",
    # incremental_index (B27)
    "IncrementalIndex",
    "ChangeType",
    "ChangeRecord",
    "FileWatermark",
    # gatherer (B31)
    "GathererQueue",
    "GathererEntry",
    "TransactionFlags",
    # prefetch (B33)
    "PredictivePrefetch",
    "AccessRecord",
    # bloom_filter
    "BloomFilter",
    "CountingBloomFilter",
    # count_min
    "CountMinSketch",
    "HeavyHitters",
    # rate_limiter
    "TokenBucket",
    "SlidingWindowCounter",
    # consistent_hash
    "ConsistentHashRing",
    # minhash_lsh
    "MinHasher",
    "LSHIndex",
    # hyperloglog
    "HyperLogLog",
    # document_ingest (B34)
    "ConverterRegistry",
    "ConversionResult",
    "Converter",
    "CSVConverter",
    "JSONConverter",
    "XMLConverter",
    "HTMLConverter",
    "TextConverter",
    "ZipConverter",
    "UnsupportedFormat",
    "default_registry",
    # flow_dag (B35)
    "FlowDefinition",
    "FlowNode",
    "FlowEval",
    "FlowResult",
    "FlowTrace",
    "FlowExecutor",
    "FlowError",
    "parse_flow",
    "detect_cycles",
    "topological_sort",
    # agent_runtime (B36)
    "Message",
    "TraceEvent",
    "AgentRuntime",
    "Agent",
    "SimpleAgent",
    "ToolAction",
    "ToolResult",
    "Workbench",
    "AgentAsTool",
    # graph_rag (B37)
    "Entity",
    "Relationship",
    "Community",
    "GraphRAGIndex",
    "extract_entities",
    "extract_relationships",
    "detect_communities",
    "build_community_summaries",
    "local_query",
    "global_query",
    "build_index",
    # extension_host (B38)
    "ExtensionManifest",
    "CommandContribution",
    "ToolContribution",
    "ConverterContribution",
    "MaintenanceCheckContribution",
    "ExtensionHost",
    "matches_activation",
    # iteration_plan (B39)
    "IterationPlan",
    "EndgameChecklist",
    "ChecklistItem",
    "CheckState",
    "IterationPlanManager",
    "default_endgame_checklist",
    # utility_registry (B40)
    "Utility",
    "UtilityRegistry",
    "default_utility_registry",
    # shell_session (B41)
    "ShellSession",
    "CommandRecord",
    "OutputFrame",
    "strip_ansi",
    "normalize_vt",
    "has_ansi",
    "check_shell_safety",
    # source_registry (B42)
    "Source",
    "SourceRegistry",
    "SourceRecord",
    "SourceType",
    # actionability (B43)
    "ActionabilityCheck",
    "ActionabilityReport",
    "ActionTrace",
    "TraceRecorder",
    "auto_wait",
    "check_exists",
    "check_visible",
    "check_stable",
    "check_enabled",
    "check_receives_input",
    # changelog (B44)
    "ChangeFile",
    "ChangelogChangeKind",
    "ReleaseNotes",
    "ChangelogManager",
    "write_change_file",
    # kernel_plugins (B45)
    "Kernel",
    "ToolSchema",
    "ToolParam",
    "kernel_function",
    "introspect_tool",
    "validate_structured_output",
    # process_framework (B46)
    "Process",
    "ProcessStep",
    "ProcessState",
    "make_bid_process",
    "make_permit_process",
    # agent_benchmark (B47)
    "AgentBenchmark",
    "AgentEnv",
    "BenchmarkScenario",
    "EpisodeResult",
    "make_find_file_scenario",
    "make_pdf_extract_scenario",
    # group_chat (B48)
    "ChatAgent",
    "GroupChatOrchestration",
    "GroupChatResult",
    "AgentMessage",
    "make_writer_reviewer",
    "make_extractor_verifier",
    "make_planner_executer",
    # B49-B60: GitHub repo research
    "QueryExpander",
    "QueryVariant",
    "SearchResult",
    "RerankedResult",
    "HashDeduplicator",
    "FileRecord",
    "HashChange",
    "ChangeKind",
    "PermissionMode",
    "PermissionManager",
    "PermissionLevel",
    "SessionFork",
    "SessionStore",
    "DAGNode",
    "DAGEdge",
    "DependencyGraph",
    "BoundaryAwareCompactor",
    "CompactionResult",
    "TokenBudgetCompiler",
    "ContextItem",
    "ContextBOM",
    "MemorySkill",
    "MemorySkillEvolver",
    "ExtensionCatalog",
    "CatalogSource",
    "ExtensionContribution",
    "AgentArena",
    "ArenaEntry",
    "ArenaResult",
    "DaemonAgent",
    "ClientSession",
    "SharedMessage",
    "LSPManager",
    "LSPClient",
    "LSPServer",
    # B61-B68: Web scraping + deep research
    "ContentExtractor",
    "ExtractedContent",
    "URLDeduplicator",
    "DeepResearchEngine",
    "DeepResearcher",
    "QueryDecomposer",
    "GapFiller",
    "EvidenceGraph",
    "EvidenceSource",
    "Claim",
    "CriticLoop",
    "BudgetGuard",
    "CriticResult",
    "CriticDimension",
    "ParallelFanOut",
    "ParallelFanout",
    "FanOutTask",
    "FanOutResult",
    "CrossSourceAnalyzer",
    "CrossSourceAnalysis",
    "ClarificationGate",
    "Clarification",
    "ClarificationResult",
    "ResearchWorkspace",
    "ResearchBranch",
    "ResearchArtifact",
    # B69-B76: Subagent/threading
    "SubAgentSpawner",
    "SubAgentConfig",
    "SubAgentResult",
    "ParallelDelegator",
    "DelegatedTask",
    "DelegationResult",
    "COWStateManager",
    "COWState",
    "TeamExecutor",
    "TeamMember",
    "TeamResult",
    "TeamStrategy",
    "AgentsAsTools",
    "AgentTool",
    "HandoffResult",
    "SagaOrchestrator",
    "Saga",
    "SagaStep",
    "SagaResult",
    "Cavecrew",
    "CrewMember",
    "CrewTask",
    "CrewRole",
    "SpawnScaleSelector",
    "SpawnScale",
    "ScaleConfig",
    # B77-B86: Coding patterns
    "CodeGraph",
    "CodeNode",
    "CodeEdge",
    "StructuralValidator",
    "Violation",
    "CircuitBreaker",
    "ShadowEditor",
    "EditValidation",
    "LSPDiagnostic",
    "ShadowLSPDiagnostic",
    "OCCEditor",
    "WriteResult",
    "RalphLoop",
    "TestRun",
    "RalphResult",
    "GoldenMaster",
    "GoldenMasterRunner",
    "RefactorTransaction",
    "Savepoint",
    "TransactionResult",
    "TaskClassifier",
    "AgentDelegator",
    "TaskClassification",
    "TaskComplexity",
    "TaskDomain",
    "CodeRank",
    "CodeRankResult",
    "RepoMapEntry",
    "rank_repo_map",
    "render_repo_map",
    "snapshot_project_graph",
    "BackpressureMonitor",
    "BackpressureController",
    "BackpressureSignal",
    "PressureLevel",
    # autonomous_engineer (optimization kernel)
    "AEConfig",
    "MemoryEngine",
    "ExecutionSandbox",
    "PerformanceWorker",
    "LLMRouter",
    "AEScheduler",
    "OptimizeSpec",
]
