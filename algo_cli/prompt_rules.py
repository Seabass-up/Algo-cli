"""Single prompt sentences shared by every injection site.

Keep navigation and summary rules here so a later prompt section cannot
reintroduce a conflicting instruction.
"""

from pathlib import Path

NAMED_PATH_RULE = (
    "When the user names a file or path, open it directly with read_file or "
    "session_slash /read. Do not call /ls first to confirm a named path. "
    "Call /ls only to discover unnamed contents, or after a direct open reports "
    "the path is missing, before telling the user it is missing."
)

SUMMARY_RULE = (
    "Do not narrate each tool call. Summarize outcomes in plain language after "
    "the work completes."
)

UNKNOWN_OUTCOME_RECOVERY = (
    "UNKNOWN_OUTCOME means the effect may have landed. Reconcile before retry: "
    "run a read-only check of that action's target and compare it with the prior state. "
    "If the observed state matches the intended postcondition, treat the action as completed. "
    "If it still matches the prior state, treat it as not applied. Otherwise stop and ask. "
    "This ledger does not name the target."
)

RUNTIME_SOURCE_RULE = (
    "The running package source is not the session workspace. "
    "git_status reports the workspace cwd only. "
    "Do not treat that status as the code that is executing."
)

DECISION_VERIFICATION_RULE = (
    "Decision validation checks schema, not factual correctness. "
    "Choice confidence is distribution concentration, not correctness; Noul and Score are advisory. "
    "Use authorized deterministic computation for exact arithmetic, probabilities, graphs and proofs. "
    "Confidence and manual answer keys are not machine verification. "
    "When checking is unauthorized, report it unverified; never bypass permissions, lower routing "
    "thresholds or relabel advice as proof."
)


def catalog_guidance(config_dir: Path | None = None) -> str:
    """Use the same configured owner catalog for every operational surface."""
    from .config import CONFIG_DIR

    return (
        f"Read and update the live algorithm/pattern catalog at {(config_dir or CONFIG_DIR) / 'ALGO.md'}. "
        "If absent, it is an unpopulated user catalog. docs/ALGO.md is only the empty "
        "public template, not the live catalog. Never publish the user's catalog or custom kernels."
    )


def current_catalog_system_prompt(system: str) -> str:
    """Remove exact product-owned duplicates; runtime renders catalog guidance once."""
    old_lines = {
        "- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md.",
        "- For Algo algorithm/pattern catalog guidance, use and update the live catalog at ~/.algo_cli/ALGO.md. docs/ALGO.md is only the empty public template, not the live catalog.",
        "- To read/update the live catalog, use the path in the runtime Algorithm Catalog section; docs/ALGO.md is only the empty public template.",
    }
    return "\n".join(line for line in system.split("\n") if line not in old_lines)


def runtime_source_root() -> Path:
    """Directory that contains the algo_cli package currently imported."""
    return Path(__file__).resolve().parents[1]
