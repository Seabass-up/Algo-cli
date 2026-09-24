"""Capability readiness: whether a discovered route will work in this session.

Discovery (action_search, available_actions) says a capability exists. This
module says whether it is usable now, through four local checks:

- supported: the code for it is present in this build and platform;
- configured: credentials, companion binaries, or config exist on disk or in the
  environment (checked locally; nothing contacts a provider or service);
- allowed_in_session: the live runtime policy would admit the call, evaluated
  with the preflight rules but without dispatching, granting, or recording;
- verified: the capability last succeeded in this session (in memory only).

Every check is bounded and local, and no public function raises.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .x_account import CONFIRMED_POST_ACTIONS as X_ACCOUNT_CONFIRMED_POST_ACTIONS

READINESS_GUIDANCE = (
    "Check readiness before offering a route. Offer only capabilities whose readiness status is 'ready' or "
    "'verified'; for 'needs_approval' say the user will be asked to approve it. For 'not_configured', "
    "'blocked', or 'unsupported', tell the user the reason and exact next_step instead of offering the route, "
    "and do not try another tool as a workaround. 'unconfirmed' means setup cannot be checked locally; say so. "
    "Jev only ranks or classifies supplied options; it cannot browse, read email, or execute actions. "
    "capability_status(topic) reports readiness for one topic."
)

_VERIFIED_ATTR = "_algo_capability_verified"
_NO_WORKAROUND = "Report this to the user; do not retry or work around it."


@dataclass(frozen=True)
class Check:
    ok: bool | None
    reason: str = ""
    next_step: str = ""

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"ok": self.ok}
        if self.reason:
            row["reason"] = self.reason
        if self.next_step:
            row["next_step"] = self.next_step
        return row


_OK = Check(True)


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    label: str
    tools: tuple[str, ...] = ()
    slash_prefixes: tuple[str, ...] = ()
    probe: tuple[str, dict[str, Any]] | None = None
    aliases: tuple[str, ...] = ()
    configured: Callable[[Any], Check] | None = None
    supported: Callable[[], Check] | None = None
    not_for: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)
    # Tools that succeed without contacting the service (local drafts); they never verify the capability.
    offline_tools: tuple[str, ...] = ()
    # For a /group command, only these subcommands reach the service; help and local drafts do not.
    verifying_subcommands: frozenset[str] | None = None


def _google_configured(_cfg: Any) -> Check:
    from . import google_workspace_auth

    status = google_workspace_auth.auth_status()
    if status.get("authenticated"):
        return _OK
    if not status.get("client_configured"):
        return Check(
            False,
            "Google OAuth client is not configured.",
            "Run `algo-cli config setup google`, then `algo-cli config auth google login`.",
        )
    if not status.get("token_present"):
        return Check(False, "Google account is not signed in.", "Run `algo-cli config auth google login`.")
    return Check(
        False,
        "Google sign-in has expired and cannot be refreshed.",
        "Run `algo-cli config auth google login` again.",
    )


def _browser_configured(_cfg: Any) -> Check:
    # The browser service is a local HTTP process; probing it would be a request,
    # so readiness reports it as unconfirmed until a call succeeds.
    url = os.environ.get("ALGO_BROWSER_URL", "").strip() or "http://localhost:9377"
    return Check(
        None,
        f"The Camoufox browser service at {url} is not probed by readiness checks.",
        "Start the Camoufox browser service (or set ALGO_BROWSER_URL); /doctor checks it.",
    )


def _jev_configured(cfg: Any) -> Check:
    from .jev_kernel import JevKernelError, companion_path

    next_step = "Run `algo-cli config jev enable --cli /absolute/path/to/jev-workflows`."
    try:
        companion_path(cfg)
    except JevKernelError as exc:
        if str(exc) == "jev_companion_not_configured":
            return Check(False, "The jev-workflows companion is not configured.", next_step)
        return Check(False, "The configured jev-workflows companion is missing or not executable.", next_step)
    except OSError:
        return Check(False, "The configured jev-workflows companion is missing or not executable.", next_step)
    if getattr(cfg, "jev_kernel_enabled", False) is not True:
        return Check(False, "Jev inference is disabled; only local lint works.", next_step)
    return _OK


def _xai_configured(_cfg: Any) -> Check:
    from . import xai_auth

    if xai_auth.api_key_configured():
        return _OK
    return Check(False, "XAI_API_KEY is not configured.", "Run `algo-cli config setup xai`.")


def _x_account_configured(_cfg: Any) -> Check:
    if shutil.which("xurl"):
        return _OK
    return Check(
        False,
        "The official xurl CLI is not on PATH.",
        "Install xurl and authenticate it outside Algo CLI (`xurl auth`), then run /x-account status.",
    )


def _web_supported() -> Check:
    import ollama

    if hasattr(ollama.Client, "web_search") and hasattr(ollama.Client, "web_fetch"):
        return _OK
    return Check(False, "The installed ollama package has no web search API.", "Upgrade to ollama>=0.5.")


def _web_configured(_cfg: Any) -> Check:
    if os.environ.get("OLLAMA_API_KEY", "").strip():
        return _OK
    return Check(
        False,
        "OLLAMA_API_KEY is not set; web tools need Ollama Cloud access.",
        "Add OLLAMA_API_KEY to ~/.algo_cli/env (or ALGO_CLI_ENV_FILE), then run /doctor.",
    )


def _embeddings_configured(cfg: Any) -> Check:
    model = str(getattr(cfg, "harness_embed_model", "") or "").strip()
    if not model:
        return Check(False, "No embedding model is configured.", "Set harness_embed_model, for example all-minilm.")
    return _OK


def _harness_configured(_cfg: Any) -> Check:
    from . import harness

    if Path(harness.INDEX_PATH).exists():
        return _OK
    return Check(False, "The harness index has not been built.", "Run /harness refresh.")


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "google",
        "Google Workspace via /google (Gmail read/drafts, Drive, Docs, Sheets, Calendar)",
        slash_prefixes=("/google",),
        probe=("session_command", {"command": "/google gmail-list"}),
        verifying_subcommands=frozenset(
            {
                "calendar-list",
                "docs-get",
                "drive-get",
                "drive-list",
                "drive-search",
                "gmail-draft",
                "gmail-get",
                "gmail-list",
                "sheets-values",
            }
        ),
        aliases=("gmail", "email", "mail", "inbox", "drive", "docs", "sheets", "calendar", "workspace"),
        configured=_google_configured,
    ),
    CapabilitySpec(
        "browser",
        "Local browser via cobalt_* tools",
        tools=(
            "cobalt_open",
            "cobalt_snapshot",
            "cobalt_screenshot",
            "cobalt_navigate",
            "cobalt_click",
            "cobalt_type",
            "cobalt_scroll",
            "cobalt_close",
        ),
        probe=("cobalt_open", {"url": "https://example.com"}),
        aliases=("browse", "browsing", "website", "webpage", "page", "cobalt", "camoufox"),
        configured=_browser_configured,
    ),
    CapabilitySpec(
        "jev",
        "Jev advisory judgments (rank, classify, score supplied options)",
        tools=("jev_kernel_status", "jev_question_contract"),
        probe=("jev_question_contract", {"contract": {}, "mode": "run"}),
        aliases=("typesafe", "rank", "ranking", "classify", "triage"),
        configured=_jev_configured,
        not_for=("browsing", "reading email", "executing actions", "writing text"),
    ),
    CapabilitySpec(
        "xai",
        "xAI Grok and x_search",
        tools=("x_search",),
        probe=("x_search", {"query": "readiness"}),
        aliases=("grok", "x_search", "live search"),
        configured=_xai_configured,
    ),
    CapabilitySpec(
        "x_account",
        "X account actions through xurl",
        tools=(
            "x_account_status",
            "x_account_draft_post",
            "x_account_draft_reply",
            "x_account_post",
            "x_account_reply",
            "x_account_post_action",
        ),
        slash_prefixes=("/x-account",),
        probe=("x_account_status", {}),
        offline_tools=("x_account_draft_post", "x_account_draft_reply"),
        verifying_subcommands=frozenset({"status", "post", "reply"} | X_ACCOUNT_CONFIRMED_POST_ACTIONS),
        aliases=("twitter", "tweet", "post", "xurl", "x account"),
        configured=_x_account_configured,
    ),
    CapabilitySpec(
        "web",
        "Web search and fetch (Ollama Cloud)",
        tools=("web_search", "web_fetch"),
        probe=("web_search", {"query": "readiness"}),
        aliases=("search", "internet", "fetch", "url", "online"),
        configured=_web_configured,
        supported=_web_supported,
    ),
    CapabilitySpec(
        "embeddings",
        "Local embeddings",
        tools=("embed_text",),
        slash_prefixes=("/embed",),
        probe=("embed_text", {"text": "readiness"}),
        aliases=("embed", "embedding", "vector", "vectors"),
        configured=_embeddings_configured,
    ),
    CapabilitySpec(
        "harness",
        "Harness index search",
        tools=("harness_search", "harness_read", "harness_stats", "harness_refresh"),
        slash_prefixes=("/harness", "/hsearch", "/hread", "/hs", "/hr"),
        probe=("harness_search", {"query": "readiness"}),
        aliases=("skills", "index", "prompts", "wiki"),
        configured=_harness_configured,
    ),
)
_BY_KEY = {spec.key: spec for spec in CAPABILITIES}
_TOOL_CAPABILITY = {tool: spec.key for spec in CAPABILITIES for tool in spec.tools}
_OFFLINE_TOOLS = frozenset(tool for spec in CAPABILITIES for tool in spec.offline_tools)


def _guarded(check: Callable[[], Check]) -> Check:
    try:
        return check()
    except Exception as exc:  # readiness must never break discovery
        return Check(False, f"readiness check failed ({type(exc).__name__})", "Run /doctor for diagnostics.")


def _configured(spec: CapabilitySpec | None, cfg: Any) -> Check:
    check = spec.configured if spec is not None else None
    if check is None:
        return _OK
    return _guarded(lambda: check(cfg))


def _slash_capability(command: str) -> str | None:
    parts = str(command or "").strip().split()
    if not parts:
        return None
    root = parts[0].casefold()
    for spec in CAPABILITIES:
        if root in spec.slash_prefixes:
            if spec.verifying_subcommands is not None:
                subcommand = parts[1].casefold() if len(parts) > 1 else ""
                return spec.key if subcommand in spec.verifying_subcommands else None
            # A bare group command only prints help; it does not exercise the capability.
            if root == "/harness" and len(parts) < 2:
                return None
            return spec.key
    return None


def record_verified(cfg: Any, name: str, args: dict[str, Any] | None = None, *, at: float | None = None) -> None:
    """Record a successful invocation. Called once from the central outcome path."""

    try:
        stamp = float(at if at is not None else time.time())
        verified = getattr(cfg, _VERIFIED_ATTR, None)
        if not isinstance(verified, dict):
            verified = {}
            setattr(cfg, _VERIFIED_ATTR, verified)
        verified[f"tool:{name}"] = stamp
        capability = None if name in _OFFLINE_TOOLS else _TOOL_CAPABILITY.get(name)
        if name in {"session_command", "session_slash"}:
            command = str((args or {}).get("command") or "")
            parts = command.split()
            if parts:
                verified[f"slash:{parts[0].casefold()}"] = stamp
            capability = _slash_capability(command)
        if capability is not None:
            verified[f"capability:{capability}"] = stamp
    except Exception:
        return


def _verified_at(cfg: Any, key: str) -> float | None:
    verified = getattr(cfg, _VERIFIED_ATTR, None)
    stamp = verified.get(key) if isinstance(verified, dict) else None
    return float(stamp) if isinstance(stamp, (int, float)) else None


def _verified_dict(stamp: float | None) -> dict[str, Any]:
    if stamp is None:
        return {"ok": False, "at": None}
    return {"ok": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp))}


def _ceiling_error(name: str, cfg: Any) -> str | None:
    from .nathan_program_runtime import ProgramAuthorization, authorization_for_actions
    from .tools import TOOL_MAP

    authorization = getattr(cfg, "_algo_program_authorization", None)
    if not isinstance(authorization, ProgramAuthorization):
        return None
    full = authorization_for_actions(tuple(TOOL_MAP)).allowed_actions
    if name == "session_command":
        if authorization.allowed_actions != full:
            return "session commands are outside this Agent Block's tool ceiling"
        return None
    if name in full and name not in authorization.allowed_actions:
        return f"{name} is outside this run's tool ceiling"
    return None


def _allowed(name: str, args: dict[str, Any], cfg: Any) -> dict[str, Any]:
    if cfg is None:
        return {"ok": None, "state": "unknown", "reason": "no runtime session is bound"}
    try:
        ceiling = _ceiling_error(name, cfg)
        if ceiling is not None:
            return {"ok": False, "state": "blocked", "reason": ceiling, "next_step": _NO_WORKAROUND}
        from .nathan_runtime import session_policy_readiness

        decision = session_policy_readiness(name, args, cfg)
    except Exception as exc:
        return {
            "ok": None,
            "state": "unknown",
            "reason": f"policy readiness check failed ({type(exc).__name__})",
        }
    row: dict[str, Any] = {
        "ok": None if decision.state == "per_call" else decision.state in {"allowed", "needs_approval"},
        "state": decision.state,
    }
    if decision.reason:
        row["reason"] = decision.reason
    if decision.next_step:
        row["next_step"] = decision.next_step
    return row


def _summarize(
    record: dict[str, Any],
    supported: Check,
    configured: Check,
    allowed: dict[str, Any],
    verified: float | None,
) -> dict[str, Any]:
    if supported.ok is False:
        status, source = "unsupported", supported
    elif configured.ok is False:
        status, source = "not_configured", configured
    elif allowed.get("ok") is False:
        status, source = "blocked", Check(False, allowed.get("reason", ""), allowed.get("next_step", ""))
    elif configured.ok is None and verified is None:
        status, source = "unconfirmed", configured
    elif allowed.get("state") == "needs_approval":
        status, source = "needs_approval", Check(True, allowed.get("reason", ""), allowed.get("next_step", ""))
    elif verified is not None:
        status, source = "verified", _OK
    else:
        status, source = "ready", _OK
    record["status"] = status
    if source.reason:
        record["reason"] = source.reason
    if source.next_step:
        record["next_step"] = source.next_step
    return record


def capability_readiness(key: str, cfg: Any = None) -> dict[str, Any]:
    """Return the four-field readiness record for one named capability."""

    spec = _BY_KEY.get(key)
    if spec is None:
        return {"capability": key, "status": "unknown", "reason": "unknown capability"}
    from .tools import TOOL_MAP

    def supported_check() -> Check:
        missing = [tool for tool in spec.tools if tool not in TOOL_MAP]
        if missing:
            return Check(False, f"not in this build: {', '.join(missing)}", "Upgrade Algo CLI.")
        return spec.supported() if spec.supported is not None else _OK

    supported = _guarded(supported_check)
    configured = _configured(spec, cfg)
    if spec.probe is not None:
        allowed = _allowed(spec.probe[0], spec.probe[1], cfg)
    else:
        allowed = {"ok": True, "state": "allowed"}
    verified = _verified_at(cfg, f"capability:{key}")
    record: dict[str, Any] = {
        "capability": key,
        "label": spec.label,
        "supported": supported.as_dict(),
        "configured": configured.as_dict(),
        "allowed_in_session": allowed,
        "verified": _verified_dict(verified),
    }
    if spec.not_for:
        record["not_for"] = list(spec.not_for)
    return _summarize(record, supported, configured, allowed, verified)


def tool_readiness(name: str, cfg: Any = None, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return readiness for one model-callable tool."""

    from .tools import TOOL_MAP

    supported = _OK if name in TOOL_MAP else Check(False, "not a registered tool in this build")
    key = _TOOL_CAPABILITY.get(name)
    spec = _BY_KEY.get(key) if key else None
    configured = _configured(spec, cfg)
    if spec is not None and spec.supported is not None and supported.ok:
        supported = _guarded(spec.supported)
    allowed = _allowed(name, dict(args or {}), cfg) if supported.ok else {"ok": False, "state": "blocked"}
    verified = _verified_at(cfg, f"tool:{name}")
    record: dict[str, Any] = {
        "supported": supported.as_dict(),
        "configured": configured.as_dict(),
        "allowed_in_session": allowed,
        "verified": _verified_dict(verified),
    }
    if key:
        record["capability"] = key
    return _summarize(record, supported, configured, allowed, verified)


def slash_readiness(command: str, cfg: Any = None) -> dict[str, Any]:
    """Return readiness for one runnable /command line routed through session_command."""

    parts = str(command or "").split()
    root = parts[0].casefold() if parts else ""
    spec = next((item for item in CAPABILITIES if root and root in item.slash_prefixes), None)
    configured = _configured(spec, cfg)
    allowed = _allowed("session_command", {"command": command}, cfg)
    verified = _verified_at(cfg, f"capability:{spec.key}" if spec is not None else f"slash:{root}")
    record: dict[str, Any] = {
        "supported": _OK.as_dict(),
        "configured": configured.as_dict(),
        "allowed_in_session": allowed,
        "verified": _verified_dict(verified),
    }
    if spec is not None:
        record["capability"] = spec.key
    return _summarize(record, _OK, configured, allowed, verified)


def all_readiness(cfg: Any = None) -> list[dict[str, Any]]:
    return [capability_readiness(spec.key, cfg) for spec in CAPABILITIES]


def compact(record: dict[str, Any]) -> dict[str, Any]:
    """One-line form used inside larger discovery payloads."""

    row = {"status": record.get("status", "unknown")}
    for field_name in ("reason", "next_step"):
        if record.get(field_name):
            row[field_name] = record[field_name]
    if record.get("not_for"):
        row["not_for"] = record["not_for"]
    return row


def matching_capabilities(topic: str) -> list[str]:
    """Capability keys whose name, tools, commands, or aliases match a topic."""

    from .tool_context import document_terms, specific_query_terms

    text = str(topic or "").strip().casefold()
    if not text:
        return [spec.key for spec in CAPABILITIES]
    terms = specific_query_terms(text)
    keys: list[str] = []
    for spec in CAPABILITIES:
        vocabulary = (spec.key, *spec.tools, *spec.slash_prefixes, *spec.aliases)
        if (
            text in {item.casefold() for item in vocabulary}
            or text in spec.key
            or any(text == prefix.lstrip("/") for prefix in spec.slash_prefixes)
            or bool(terms & document_terms(" ".join((spec.key, spec.label, *spec.aliases))))
        ):
            keys.append(spec.key)
    return keys


def capability_status_payload(topic: str | None, cfg: Any = None) -> dict[str, Any]:
    """Payload for the capability_status tool and the /capabilities command."""

    from .tools import TOOL_MAP

    focus = str(topic or "").strip()
    if focus and focus in TOOL_MAP:
        return {
            "topic": focus,
            "match": "tool",
            "tools": {focus: tool_readiness(focus, cfg)},
            "guidance": READINESS_GUIDANCE,
        }
    keys = matching_capabilities(focus)
    if not keys:
        return {
            "topic": focus,
            "match": "none",
            "capabilities": [],
            "topics": [spec.key for spec in CAPABILITIES],
            "next": (
                "No tracked capability matched this topic. This is not proof it is unavailable; call "
                "capability_status() for all tracked capabilities or action_search(query) for tools."
            ),
            "guidance": READINESS_GUIDANCE,
        }
    return {
        "topic": focus or "all",
        "match": "matched",
        "capabilities": [capability_readiness(key, cfg) for key in keys],
        "guidance": READINESS_GUIDANCE,
    }


_STATUS_STYLE = {
    "ready": "success",
    "verified": "success",
    "needs_approval": "warning",
    "unconfirmed": "warning",
    "not_configured": "error",
    "blocked": "error",
    "unsupported": "error",
}


def _mark(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "?"


def render_capability_table(records: list[dict[str, Any]]) -> Any:
    """Compact Rich table for /capabilities."""

    from rich import box
    from rich.table import Table
    from rich.text import Text

    table = Table(title="Capability readiness", box=box.SIMPLE_HEAD, show_lines=False, expand=False)
    for column in ("Capability", "Status", "Sup", "Conf", "Allowed", "Verified", "Next step"):
        table.add_column(column, overflow="fold")
    for record in records:
        allowed = record.get("allowed_in_session", {})
        verified = record.get("verified", {})
        status = str(record.get("status", "unknown"))
        table.add_row(
            Text(str(record.get("capability", ""))),
            Text(status, style=_STATUS_STYLE.get(status, "muted")),
            Text(_mark(record.get("supported", {}).get("ok"))),
            Text(_mark(record.get("configured", {}).get("ok"))),
            Text(str(allowed.get("state", "?"))),
            Text(str(verified.get("at") or "-")),
            Text(str(record.get("next_step") or "")),
        )
    return table
