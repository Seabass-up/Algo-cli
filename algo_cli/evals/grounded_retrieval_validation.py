"""Public development validation labels, frozen before their rankings are run.

This extends topic and multi-result coverage, not an independent held-out test.
It deliberately includes distinct tools and patterns from the same source file.
"""

from .grounded_retrieval import AUTH, ECHO, EXTERNAL, PRIVACY, REVIEW, Case, Evidence, _doc


def _capability(name: str) -> Evidence:
    return Evidence(f"algo-cli:runtime_capability:{name}", f"Capability {name}.")


def _pattern(name: str, title: str) -> Evidence:
    return Evidence(f"algo-cli:algorithm:ALGO.md#{name}", title)


O4 = _pattern("O4", "Prerequisite-Partitioned Diagnostic Probes")
O5 = _pattern("O5", "Source-Bound Pattern-Level Retrieval")
O6 = _pattern("O6", "Executable Pattern Evidence Registry")
O7 = _pattern("O7", "Paired Pattern and Interaction Comparisons")
O8 = _pattern("O8", "Applicability-First Compatible Selection")

CASES = (
    Case(
        "file_tools",
        "explicit_multi_result",
        "Compare write_file and edit_file runtime capabilities",
        (_capability("write_file"), _capability("edit_file")),
    ),
    Case(
        "git_tools",
        "explicit_multi_result",
        "Find git_status and git_diff capability records",
        (_capability("git_status"), _capability("git_diff")),
    ),
    Case(
        "web_tools",
        "explicit_multi_result",
        "web_search and web_fetch network and provider prerequisites",
        (_capability("web_search"), _capability("web_fetch")),
    ),
    Case(
        "filtered_file_tools",
        "filtered_multi_result",
        "write_file edit_file approval requirements",
        (_capability("write_file"), _capability("edit_file")),
        "runtime_capability",
    ),
    Case("diagnostic_and_retrieval", "explicit_multi_result", "ALGO patterns O4 and O5", (O4, O5)),
    Case("registry_and_comparison", "explicit_multi_result", "ALGO patterns O6 and O7", (O6, O7)),
    Case(
        "comparison_and_selection",
        "filtered_multi_result",
        "O7 Paired Pattern and Interaction Comparisons and O8 Applicability-First Compatible Selection",
        (O7, O8),
        "algorithm",
    ),
    Case("pattern_title", "exact", "Source-Bound Pattern-Level Retrieval", (O5,), "algorithm"),
    Case("retry_limit", "exact", "HTTP 502 request opening retry budget two retries", (AUTH,)),
    Case(
        "certificate_error",
        "paraphrase",
        "Should the client keep reconnecting when TLS certificate verification fails?",
        (AUTH,),
    ),
    Case(
        "review_failure",
        "paraphrase",
        "What happens if the private terminal approval reviewer disconnects before I approve?",
        (REVIEW,),
    ),
    Case("review_terminal", "exact", "--review-actions controlling terminal inherited descriptor", (REVIEW,)),
    Case(
        "cloud_privacy_fr",
        "multilingual",
        "Mes extraits de code restent-ils sur mon Mac avec un modele distant?",
        (PRIVACY,),
    ),
    Case("cloud_privacy_de", "multilingual", "Werden lokale Dateien an den Cloud-Anbieter geschickt?", (PRIVACY,)),
    Case(
        "echo_hardware",
        "paraphrase",
        "Is hardware isolation or remote attestation active for this encrypted memory store?",
        (ECHO,),
    ),
    Case(
        "store_opt_in",
        "paraphrase",
        "Do supported external agent stores become available before I configure and enable them?",
        (EXTERNAL,),
    ),
    Case(
        "plugin_metadata",
        "exact",
        "Structured Codex plugin metadata indexing plugin.json",
        (_doc("harness-extension-cleanup-recommendation", "plugin.json"),),
    ),
    Case(
        "graph_integration",
        "exact",
        "Index Compute Lab integration query_knowledge_graph",
        (_doc("index-compute-lab-integration", "query_knowledge_graph"),),
    ),
    Case(
        "auth_and_privacy",
        "multi_evidence",
        "Provider Authentication Recovery and Privacy and local context",
        (AUTH, PRIVACY),
    ),
)

# v1 incorrectly counted a historical design document as a positive label.
# Preserve the exact query and anchor as a separate exclusion check; this is
# an inventory correction, not a semantic-ranking improvement.
EXCLUSION_CASES = (Case("reflex_spec", "exact", "Reflex Loop Spec v0.2", (_doc("reflex-loop-v0.2", "Reflex Loop"),)),)

# Frozen before the source-selection challenge was run; public development
# cases, not independent held-out evidence. Require every listed result.
CHALLENGE_CASES = (
    Case(
        "retrieval_methods",
        "same_source_comparison",
        "Compare BM25 plus vector similarity with reciprocal rank fusion",
        (_pattern("A1", "Hybrid Retrieval"), _pattern("A2", "Reciprocal Rank Fusion")),
    ),
    Case(
        "cache_methods",
        "same_source_comparison",
        "Contrast model-aware LRU query embedding cache and Window TinyLFU cache admission",
        (_pattern("A4", "Model-Aware LRU"), _pattern("L1", "Window TinyLFU")),
    ),
    Case(
        "retry_coalescing",
        "same_source_comparison",
        "Compare adaptive retry with exponential backoff and request coalescing for cache miss storms",
        (_pattern("B467", "Adaptive Retry"), _pattern("B468", "Request Coalescing")),
    ),
    Case(
        "merkle_build",
        "same_source_comparison",
        "Describe Merkle tree index integrity and incremental build cache",
        (_pattern("B17", "Merkle Tree"), _pattern("Q9", "Incremental Build Cache")),
    ),
    Case(
        "named_source",
        "same_source_comparison",
        "Select ALGO strategies for Token-Budget Knapsack and Window TinyLFU Cache Admission",
        (_pattern("A10", "Token-Budget Knapsack"), _pattern("L1", "Window TinyLFU")),
    ),
    Case(
        "natural_file_tools",
        "natural_multi_tool",
        "Which tools create a file, apply an edit to an existing file, and show a working-tree diff?",
        (_capability("write_file"), _capability("edit_file"), _capability("git_diff")),
    ),
    Case(
        "natural_web_tools",
        "natural_multi_tool",
        "Which tools search the web and retrieve the contents of a known URL?",
        (_capability("web_search"), _capability("web_fetch")),
    ),
    Case(
        "natural_git_tools",
        "natural_multi_tool",
        "Which tools inspect repository status and show the current git diff?",
        (_capability("git_status"), _capability("git_diff")),
    ),
)
