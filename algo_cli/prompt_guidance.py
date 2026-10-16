"""Optional task tutorials; never an action or permission selector."""

from __future__ import annotations

import re
from typing import Any

from .chat_protocol import get_attr


PDF_GUIDANCE = (
    "## PDF Handling\n"
    "For PDF files, call read_pdf first. Do not claim Python is unavailable and do not improvise shell-based PDF extraction before trying read_pdf. "
    "If read_pdf says the document is scanned or image-only, call render_pdf_pages next and then pass one returned PNG path to vision_describe or another OCR-capable path. "
    "If a vision/OCR model returns unsupported-image or insufficient-memory errors, do not repeat the same model across more PDF pages or sibling PDFs without changing the model or approach."
)
XAI_GUIDANCE = (
    "## Grok / xAI model compatibility\n"
    "When the user asks whether a grok-* model works in this harness, do not scan the repo blindly: "
    "tell them to run /model-check NAME (or run it yourself via session slash if available), "
    "and search the currently enabled harness for Algo CLI xAI/X-account guidance; do not assume external record IDs exist. "
    "Multi-agent grok models use xAI /v1/responses; other Grok models use /v1/chat/completions. "
    "Auth uses the documented XAI_API_KEY flow. Tell users to run `algo-cli config setup xai`; do not request, "
    "display, or persist key material in conversation. xAI API calls may consume paid usage."
)


def task_tool_names(messages: list[dict[str, Any]], task: str) -> set[str]:
    # Bind to this exact user turn, not a previous task or quoted tool output.
    start = next((index for index in range(len(messages) - 1, -1, -1)
                  if messages[index].get("role") == "user" and messages[index].get("content") == task), len(messages))
    names = set()
    for message in messages[start:]:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                name = get_attr(get_attr(call, "function", {}), "name", "")
                if type(name) is str and name:
                    names.add(name)
    return names


def task_guidance(task: str, messages: list[dict[str, Any]], *, xai: bool,
                  observed_tools: set[str] | None = None) -> dict[str, str]:
    """Select explanations from observable intent/use, without changing tools."""
    tools = task_tool_names(messages, task) | (observed_tools or set())
    selected = {}
    if re.search(r"\bpdfs?\b", task, re.I) or tools & {"read_pdf", "render_pdf_pages", "vision_describe"}:
        selected["pdf"] = PDF_GUIDANCE
    if xai or re.search(r"\b(?:grok|xai)\b|x\.ai\b", task, re.I):
        selected["xai"] = XAI_GUIDANCE
    if (re.search(r"\b(?:harness|skills?|wiki|notes|kernels?|catalog|patterns?)\b", task, re.I)
            or any(name.startswith("harness_") for name in tools)):
        selected["harness"] = (
            "## Local Harness Bridge\n"
            "Use available_actions, harness_search, harness_read, harness_stats, and harness_refresh. "
            "These tools are read-only and should be preferred before broad filesystem scans when the user asks about skills, tools, prompts, memory, or wiki context. "
            "Relevant Context (if present) already contains selected reference entries; use harness_search/harness_read for missing evidence or an explicit deep dive."
        )
    return selected
