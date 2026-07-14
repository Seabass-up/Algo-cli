"""Built-in Agent Block pipeline definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text
from .tool_policy import TOOL_GROUP_MAP, expand_tool_groups

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.10: fall back to the tomli backport.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - tomli is a declared 3.10 dependency.
        tomllib = None  # type: ignore[assignment]


NO_TOOLS: frozenset[str] = frozenset()
PLAN_TOOLS: frozenset[str] = expand_tool_groups(["read"])
READ_TOOLS: frozenset[str] = expand_tool_groups(["read", "web"])
IMPLEMENT_TOOLS: frozenset[str] = READ_TOOLS | expand_tool_groups(["write", "shell"])
REVIEW_TOOLS: frozenset[str] = READ_TOOLS | expand_tool_groups(["shell"])
DEFAULT_OUTPUT_LIMIT_CHARS = 3000
BLOCKS_CONFIG_PATH = CONFIG_DIR / "blocks.toml"
MAX_BLOCK_ITERATIONS = 24
REQUIRED_CHANGE_PROMPT = (
    "File mutation policy (required-change block):\n"
    "- You MUST use edit_file (find/replace) for surgical changes to existing files, and "
    "write_file only for new files or full rewrites. Edit_file is faster and more "
    "reliable than rewriting whole files.\n"
    "- You MUST use write_file for any new file creation. Call write_file without "
    "overwrite=True; that flag is only for overwriting existing files.\n"
    "- When using edit_file, first call read_file to confirm the exact text, then include "
    "2-5 lines of surrounding context in old_string so the match is unique. If the tool "
    "reports an ambiguous match, tighten old_string with more context or pass "
    "replace_all=True only when the rewrite is intentionally global.\n"
    "- You MUST NOT use run_shell to create, edit, append to, or delete files. This includes "
    "heredocs, output redirection (`> file`, `>> file`), `sed -i`, `awk -i`, `echo > file`, "
    "`Set-Content`, `Add-Content`, `Out-File`, `New-Item -ItemType File`, `cp`/`mv` into the "
    "workspace, and any other shell-based mutation. Shell-based file mutation will not be "
    "counted as evidence of a change.\n"
    "- Shell is permitted only for read-only verification (status, tests, lint, diff, grep, ls). "
    "Direct Git or file mutations via shell require explicit approval and are not the preferred path.\n"
    "- If edit_file and write_file are both unavailable or refuse, stop and report that the change cannot be made "
    "within policy rather than routing edits through shell."
)


class BlocksConfigError(ValueError):
    """Raised when a configured Agent Blocks pipeline cannot be used."""


@dataclass
class AgentBlock:
    role: str
    prompt: str
    allowed_tools: frozenset[str] = NO_TOOLS
    model: str | None = None
    max_iterations: int = 8
    requires_change: bool = False
    status: str = "pending"
    status_code: str = ""
    status_reason: str = ""
    output: str = ""
    tool_calls: int = 0
    duration_ms: float = 0.0
    messages: list[dict] = field(default_factory=list)
    context_output: str = ""
    successful_writes: list[str] = field(default_factory=list)
    git_evidence: str = ""
    git_head: str = ""
    git_status: str = ""
    status_digest: str = ""
    tracked_diff_digest: str = ""
    untracked_digest: str = ""
    git_clean: bool = False
    verification_warning: str = ""
    failed_writes: list[str] = field(default_factory=list)
    mutation_denied: bool = False
    mutation_actions: list[str] = field(default_factory=list)
    audit_evidence: str = ""


def _prompt(role: str, body: str) -> str:
    tools = (
        "You may use only the tools allowed for this block. "
        "If no tools are available, reason from the supplied context only."
    )
    return (
        f"You are the {role} block in a sequential terminal-agent pipeline.\n"
        f"{tools}\n"
        "Keep the output concise and directly useful to the next block.\n"
        "When you are done, return Markdown starting with exactly:\n"
        "## Block Output\n\n"
        f"{body}"
    )


def default_pipeline() -> list[AgentBlock]:
    return [
        AgentBlock(
            role="plan",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "plan",
                "Create a short execution plan for the user's task. Identify assumptions, risks, and concrete next steps.",
            ),
        ),
        AgentBlock(
            role="research",
            allowed_tools=READ_TOOLS,
            prompt=_prompt(
                "research",
                "Gather only the information needed to execute the plan. Prefer local files and harness context before broad searches.",
            ),
        ),
        AgentBlock(
            role="implement",
            allowed_tools=IMPLEMENT_TOOLS,
            requires_change=True,
            prompt=_prompt(
                "implement",
                "Perform the implementation or operational work. For any file creation, "
                "use write_file. For surgical edits to an existing file, use edit_file "
                "(find/replace) with 2-5 lines of surrounding context in old_string to "
                "make the match unique. Do not use run_shell to write, edit, append to, or "
                "delete files; shell is for verification only.",
            ),
        ),
        AgentBlock(
            role="review",
            allowed_tools=REVIEW_TOOLS,
            prompt=_prompt(
                "review",
                "Review the implementation for correctness, safety, regressions, and missing verification. "
                "For large workspaces, sample representative high-risk files and directories first; "
                "do not exhaustively enumerate the full tree unless the user explicitly requests it. "
                "Once you have enough evidence for concrete findings, stop searching and write the block output. "
                "Never claim an implementation occurred without successful write evidence or independently verified changed file contents.",
            ),
        ),
        AgentBlock(
            role="final",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "final",
                "Produce the final user-facing answer from the previous block outputs. Be concise and mention verification performed. "
                "Do not state that files changed unless the supplied evidence confirms it.",
            ),
        ),
    ]


def code_change_pipeline() -> list[AgentBlock]:
    return [
        AgentBlock(
            role="plan",
            allowed_tools=PLAN_TOOLS,
            max_iterations=4,
            prompt=_prompt(
                "plan",
                "Inspect the local project structure as needed, then create a short implementation plan "
                "grounded in actual file paths. Identify likely risks and verification steps.",
            ),
        ),
        AgentBlock(
            role="implement",
            allowed_tools=IMPLEMENT_TOOLS,
            max_iterations=12,
            requires_change=True,
            prompt=_prompt(
                "implement",
                "Perform the requested code or configuration change. Inspect only what is needed to locate the edit, "
                "then execute it with edit_file (preferred) or write_file. "
                "For edit_file: call read_file first to confirm the exact text, then pass old_string with 2-5 lines "
                "of surrounding context to make the match unique. If the match is ambiguous, tighten old_string or "
                "pass replace_all=True only when the rewrite is intentionally global. "
                "For write_file: use overwrite=True only when the file already exists and you intend to replace it. "
                "Do not use run_shell to write, edit, append to, or delete files; shell-based file mutation does not count "
                "as a verified change. After writing, re-read the changed section and run focused verification where practical.",
            ),
        ),
        AgentBlock(
            role="review",
            allowed_tools=REVIEW_TOOLS,
            prompt=_prompt(
                "review",
                "Review the change for correctness, safety, regressions, and missing verification. "
                "For large workspaces, sample the relevant surface and prioritize concrete findings "
                "over exhaustive directory enumeration. Stop searching once findings can be supported. "
                "Never claim files changed unless Git evidence or successful write evidence is present and the changed content is verified.",
            ),
        ),
        AgentBlock(
            role="final",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "final",
                "Produce the final user-facing answer from the previous block outputs. Include changed files and verification performed. "
                "Do not state that files changed unless implementation Git/write evidence and review confirmation support it.",
            ),
        ),
    ]


def research_pipeline() -> list[AgentBlock]:
    return [
        AgentBlock(
            role="plan",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "plan",
                "Create a short research plan. Identify the facts to verify and the most reliable sources to inspect.",
            ),
        ),
        AgentBlock(
            role="research",
            allowed_tools=READ_TOOLS,
            prompt=_prompt(
                "research",
                "Gather and compare the information needed to answer the task. Prefer primary sources and local evidence when available.",
            ),
        ),
        AgentBlock(
            role="final",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "final",
                "Produce a concise final answer from the research output. Call out uncertainty and source limits.",
            ),
        ),
    ]


def review_pipeline() -> list[AgentBlock]:
    return [
        AgentBlock(
            role="review",
            allowed_tools=REVIEW_TOOLS,
            prompt=_prompt(
                "review",
                "Review the supplied target for correctness, safety, regressions, and missing tests. "
                "Lead with concrete findings. For large repositories or knowledge bases, sample "
                "representative high-risk areas rather than exhaustively listing every file. "
                "Stop inspecting once the principal findings are evidenced. "
                "Do not claim changes were made unless verified in the target files.",
            ),
        ),
        AgentBlock(
            role="final",
            allowed_tools=NO_TOOLS,
            prompt=_prompt(
                "final",
                "Produce the final review summary from the reviewer output. Keep findings first and avoid unrelated commentary.",
            ),
        ),
    ]


PIPELINE_FACTORIES = {
    "default": default_pipeline,
    "code-change": code_change_pipeline,
    "research": research_pipeline,
    "review": review_pipeline,
}

STARTER_CONFIG = """version = 1

[pipelines.default]
description = "Standard implementation workflow"

[[pipelines.default.blocks]]
role = "plan"
prompt = "Create a short execution plan for the user's task. Identify assumptions, risks, and concrete next steps."
tools = []
max_iterations = 8

[[pipelines.default.blocks]]
role = "research"
prompt = "Gather only the information needed to execute the plan. Prefer local files and harness context before broad searches."
tools = ["read", "web"]
max_iterations = 8

[[pipelines.default.blocks]]
role = "implement"
prompt = "Perform the implementation or operational work. For new files, use write_file. For surgical edits to existing files, use edit_file (find/replace) with 2-5 lines of surrounding context in old_string to make the match unique. Do not use run_shell to write, edit, append to, or delete files; shell is for verification only."
tools = ["read", "web", "write", "shell"]
max_iterations = 8
requires_change = true

[[pipelines.default.blocks]]
role = "review"
prompt = "Review the implementation for correctness, safety, regressions, and missing verification. For large workspaces, sample representative high-risk areas rather than exhaustively enumerating the full tree. Stop searching once findings can be supported. Never claim an implementation occurred without successful write evidence or independently verified changed file contents."
tools = ["read", "web", "shell"]
max_iterations = 8

[[pipelines.default.blocks]]
role = "final"
prompt = "Produce the final user-facing answer from previous block outputs. Mention verification performed. Do not state that files changed unless the supplied evidence confirms it."
tools = []
max_iterations = 8

[pipelines.code-change]
description = "Implementation workflow without a dedicated research pass"

[[pipelines.code-change.blocks]]
role = "plan"
prompt = "Inspect the local project structure as needed, then create a short implementation plan grounded in actual file paths. Identify likely risks and verification steps."
tools = ["read"]
max_iterations = 4

[[pipelines.code-change.blocks]]
role = "implement"
prompt = "Perform the requested code or configuration change. Inspect only what is needed to locate the edit, then execute it with edit_file (preferred) or write_file. For edit_file: call read_file first to confirm the exact text, then pass old_string with 2-5 lines of surrounding context to make the match unique. If the match is ambiguous, tighten old_string or pass replace_all=True only when the rewrite is intentionally global. For write_file: use overwrite=True only when the file already exists and you intend to replace it. Do not use run_shell to write, edit, append to, or delete files; shell-based file mutation does not count as a verified change. After writing, re-read the changed section and run focused verification where practical."
tools = ["read", "web", "write", "shell"]
max_iterations = 12
requires_change = true

[[pipelines.code-change.blocks]]
role = "review"
prompt = "Review the change for correctness, safety, regressions, and missing verification. For large workspaces, sample relevant high-risk areas rather than exhaustively enumerating the full tree. Stop searching once findings can be supported. Never claim files changed unless Git evidence or successful write evidence is present and the changed content is verified."
tools = ["read", "web", "shell"]
max_iterations = 8

[[pipelines.code-change.blocks]]
role = "final"
prompt = "Produce the final user-facing answer with changed files and verification performed. Do not state that files changed unless implementation Git/write evidence and review confirmation support it."
tools = []
max_iterations = 8

[pipelines.research]
description = "Focused research workflow"

[[pipelines.research.blocks]]
role = "plan"
prompt = "Create a short research plan and identify reliable sources to inspect."
tools = []
max_iterations = 8

[[pipelines.research.blocks]]
role = "research"
prompt = "Gather and compare information needed to answer the task."
tools = ["read", "web"]
max_iterations = 8

[[pipelines.research.blocks]]
role = "final"
prompt = "Produce a concise final answer and call out uncertainty or source limits."
tools = []
max_iterations = 8

[pipelines.review]
description = "Focused review workflow"

[[pipelines.review.blocks]]
role = "review"
prompt = "Review the target for correctness, safety, regressions, and missing tests. Lead with findings and sample representative high-risk areas for large workspaces. Stop inspecting once principal findings are evidenced. Do not claim changes were made unless verified in the target files."
tools = ["read", "web", "shell"]
max_iterations = 8

[[pipelines.review.blocks]]
role = "final"
prompt = "Produce the final review summary with findings first."
tools = []
max_iterations = 8
"""


def pipeline_names() -> list[str]:
    return sorted(PIPELINE_FACTORIES)


def builtin_pipeline_by_name(name: str) -> list[AgentBlock]:
    key = (name or "default").strip().lower()
    try:
        pipeline = PIPELINE_FACTORIES[key]()
        _validate_pipeline_contract(key, pipeline)
        return pipeline
    except KeyError as exc:
        available = ", ".join(pipeline_names())
        raise ValueError(f"Unknown pipeline '{name}'. Available: {available}") from exc


def _validate_pipeline_contract(pipeline_name: str, blocks: list[AgentBlock], *, start_index: int = 0) -> None:
    for index, block in enumerate(blocks, start=start_index):
        if block.requires_change and "write_file" not in block.allowed_tools:
            raise BlocksConfigError(
                f"Error in blocks.toml: pipeline '{pipeline_name}', block {index + 1} has "
                "requires_change=true but does not include the 'write' tool group. "
                "requires_change blocks must be able to use write_file."
            )


def _validate_block(pipeline_name: str, index: int, data: Any) -> AgentBlock:
    label = f"pipeline '{pipeline_name}', block {index + 1}"
    if not isinstance(data, dict):
        raise BlocksConfigError(f"Error in blocks.toml: {label} must be a table.")
    role = data.get("role")
    prompt = data.get("prompt")
    groups = data.get("tools", [])
    max_iterations = data.get("max_iterations", 8)
    model = data.get("model")
    requires_change = data.get("requires_change", False)
    if not isinstance(role, str) or not role.strip():
        raise BlocksConfigError(f"Error in blocks.toml: {label} requires a non-empty role.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BlocksConfigError(f"Error in blocks.toml: {label} requires a non-empty prompt.")
    if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
        raise BlocksConfigError(f"Error in blocks.toml: {label} tools must be a list of tool-group names.")
    unknown = sorted(set(groups) - set(TOOL_GROUP_MAP))
    if unknown:
        available = ", ".join(sorted(TOOL_GROUP_MAP))
        raise BlocksConfigError(
            f"Error in blocks.toml: {label} has unknown tool group '{unknown[0]}'. Available: {available}."
        )
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= MAX_BLOCK_ITERATIONS:
        raise BlocksConfigError(
            f"Error in blocks.toml: {label} max_iterations must be an integer from 1 to {MAX_BLOCK_ITERATIONS}."
        )
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise BlocksConfigError(f"Error in blocks.toml: {label} model must be a non-empty string when set.")
    if not isinstance(requires_change, bool):
        raise BlocksConfigError(f"Error in blocks.toml: {label} requires_change must be true or false.")
    block = AgentBlock(
        role=role.strip(),
        prompt=_prompt(role.strip(), prompt.strip()),
        allowed_tools=expand_tool_groups(groups),
        model=model.strip() if isinstance(model, str) else None,
        max_iterations=max_iterations,
        requires_change=requires_change,
    )
    _validate_pipeline_contract(pipeline_name, [block], start_index=index)
    return block


def load_configured_pipelines(path: Path | None = None) -> dict[str, list[AgentBlock]]:
    config_path = path or BLOCKS_CONFIG_PATH
    if not config_path.exists():
        return {}
    if tomllib is None:
        raise BlocksConfigError("blocks.toml requires Python 3.11+ tomllib; using built-in pipelines.")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BlocksConfigError(f"Error in blocks.toml: {exc}") from exc
    if data.get("version") != 1:
        raise BlocksConfigError("Error in blocks.toml: version must be 1.")
    pipelines = data.get("pipelines")
    if not isinstance(pipelines, dict) or not pipelines:
        raise BlocksConfigError("Error in blocks.toml: at least one [pipelines.NAME] table is required.")
    unknown_names = sorted(set(pipelines) - set(PIPELINE_FACTORIES))
    if unknown_names:
        available = ", ".join(pipeline_names())
        raise BlocksConfigError(
            f"Error in blocks.toml: unsupported pipeline '{unknown_names[0]}'. Configurable names: {available}."
        )
    configured: dict[str, list[AgentBlock]] = {}
    for name, pipeline_data in pipelines.items():
        if not isinstance(pipeline_data, dict):
            raise BlocksConfigError(f"Error in blocks.toml: pipeline '{name}' must be a table.")
        blocks = pipeline_data.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise BlocksConfigError(f"Error in blocks.toml: pipeline '{name}' requires at least one block.")
        configured[name] = [_validate_block(name, index, block) for index, block in enumerate(blocks)]
    return configured


def resolve_pipeline(name: str, path: Path | None = None) -> tuple[list[AgentBlock], str]:
    key = (name or "default").strip().lower()
    configured = load_configured_pipelines(path)
    if key in configured:
        return configured[key], "configured"
    return builtin_pipeline_by_name(key), "built-in"


def pipeline_by_name(name: str, path: Path | None = None) -> list[AgentBlock]:
    return resolve_pipeline(name, path)[0]


def write_starter_config(path: Path | None = None) -> Path:
    config_path = path or BLOCKS_CONFIG_PATH
    if config_path.exists():
        raise FileExistsError(f"{config_path} already exists. Edit it directly or remove it before running /agent init.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(config_path, STARTER_CONFIG)
    return config_path


def allowed_tool_functions(block: AgentBlock, tool_map: dict[str, object]) -> list[object]:
    return [tool_map[name] for name in sorted(block.allowed_tools) if name in tool_map]


def pipeline_context(task: str, completed: list[AgentBlock]) -> str:
    parts = [f"## Original Task\n{task.strip()}"]
    for block in completed:
        output = (block.context_output or block.output).strip() or "(no output produced)"
        parts.append(f"## Output from {block.role}\n{output}")
        if block.status_reason:
            parts.append(
                "## Block Status\n"
                f"Status: {block.status.upper()}\n"
                f"Code: {block.status_code or '-'}\n"
                f"Reason: {block.status_reason}"
            )
        if block.verification_warning:
            parts.append(
                "## Verification\n"
                f"{block.verification_warning}"
            )
        if block.requires_change:
            writes = ", ".join(block.successful_writes) if block.successful_writes else "(none recorded)"
            parts.append(
                "## Implementation Write Evidence\n"
                f"Successful write_file targets: {writes}\n"
                "Do not claim requested code changes were completed without confirming this evidence and the resulting file contents."
            )
            parts.append(
                "## Implementation Git Evidence\n"
                f"{block.git_evidence or 'No Git evidence snapshot was captured.'}\n"
                "Treat only changes introduced during the implement block as verified implementation evidence."
            )
        elif block.audit_evidence:
            parts.append(
                "## Mutation Audit Evidence\n"
                f"{block.audit_evidence}\n"
                "This is audit-only evidence; it does not change the block status or prove requested implementation."
            )
    return "\n\n".join(parts)


def compact_block_output(output: str, limit_chars: int = DEFAULT_OUTPUT_LIMIT_CHARS) -> str:
    """Compact block output for downstream context while preserving paragraphs."""
    text = (output or "").strip()
    if len(text) <= limit_chars:
        return text

    kept: list[str] = []
    total = 0
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        next_total = total + len(paragraph) + (2 if kept else 0)
        if next_total > limit_chars:
            break
        kept.append(paragraph)
        total = next_total

    compacted = "\n\n".join(kept).strip()
    if not compacted:
        compacted = text[:limit_chars].rstrip()
    return f"{compacted}\n\n... (compacted from {len(text)} chars, {len(text.split())} words)"
