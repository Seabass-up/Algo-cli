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
    "Decision schema validation checks types, labels, bounds and distributions, not factual correctness. "
    "Choice confidence is distribution concentration, not a calibrated correctness guarantee; "
    "Noul and Score outputs are advisory estimates, not exact calculators. "
    "Use authorized deterministic computation for exact arithmetic, probabilities, graph algorithms "
    "and formal deductions; use decision models to compare bounded evidence and computed candidates. "
    "A high-confidence decision or a manually checked answer key is not independent machine verification. "
    "If the required computation cannot be performed under current permissions, report it unverified; "
    "do not bypass the permission boundary, lower routing thresholds or relabel an advisory as proof."
)


def runtime_source_root() -> Path:
    """Directory that contains the algo_cli package currently imported."""
    return Path(__file__).resolve().parents[1]
