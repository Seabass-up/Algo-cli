"""URL scheme handler for Algo CLI.

Handles algo-cli:// deep links, inspired by Ollama's ollama:// URL scheme.

Supported routes:
  algo-cli://skill/<name>           — load and display a skill
  algo-cli://memory/recall?q=<query> — trigger a memory recall search
  algo-cli://session/new             — start a new session
  algo-cli://session/load?name=<n>   — load a saved session
  algo-cli://plugin/<name>           — show plugin status
  algo-cli://version                 — show full version manifest
  algo-cli://credential/<helper>     — show credential helper status

The handler parses the URL and returns a ParsedDeepLink describing the
action to take. The caller (CLI entry point or GUI companion) is responsible
for executing the action.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

URL_SCHEME = "algo-cli"


@dataclass(frozen=True)
class ParsedDeepLink:
    """A parsed algo-cli:// deep link."""
    route: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    raw_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "path": self.path,
            "query": self.query,
            "raw_url": self.raw_url,
        }


# Supported routes and their expected path/query parameters
KNOWN_ROUTES: dict[str, dict[str, Any]] = {
    "skill": {"path_required": True, "description": "Load and display a skill by name"},
    "memory": {"path_required": True, "description": "Memory operations (recall, list)"},
    "session": {"path_required": True, "description": "Session operations (new, load)"},
    "plugin": {"path_required": True, "description": "Show plugin status by name"},
    "version": {"path_required": False, "description": "Show full version manifest"},
    "credential": {"path_required": True, "description": "Show credential helper status"},
}


def parse_deep_link(url: str) -> ParsedDeepLink | None:
    """Parse an algo-cli:// URL into a ParsedDeepLink.

    Returns None if the URL is not a valid algo-cli:// link.
    """
    if not url:
        return None

    # Accept both algo-cli:// and algo-cli: for flexibility
    if url.startswith(f"{URL_SCHEME}://"):
        remainder = url[len(f"{URL_SCHEME}://"):]
    elif url.startswith(f"{URL_SCHEME}:"):
        remainder = url[len(f"{URL_SCHEME}:"):]
    else:
        return None

    # Strip leading slashes
    remainder = remainder.lstrip("/")

    # Parse query string if present
    if "?" in remainder:
        path_part, query_part = remainder.split("?", 1)
        query = dict(urllib.parse.parse_qsl(query_part))
    else:
        path_part = remainder
        query = {}

    path_part = path_part.rstrip("/")
    if not path_part:
        return ParsedDeepLink(route="", path="", query=query, raw_url=url)

    # Split into route and sub-path
    parts = path_part.split("/", 1)
    route = parts[0].lower()
    sub_path = parts[1] if len(parts) > 1 else ""

    return ParsedDeepLink(
        route=route,
        path=sub_path,
        query=query,
        raw_url=url,
    )


def validate_deep_link(link: ParsedDeepLink) -> tuple[bool, str]:
    """Validate a parsed deep link. Returns (is_valid, error_message)."""
    if not link.route:
        return False, "Empty route"

    if link.route not in KNOWN_ROUTES:
        known = ", ".join(sorted(KNOWN_ROUTES.keys()))
        return False, f"Unknown route '{link.route}'. Known routes: {known}"

    route_info = KNOWN_ROUTES[link.route]
    if route_info["path_required"] and not link.path:
        return False, f"Route '{link.route}' requires a path component"

    return True, ""


def handle_deep_link(url: str) -> dict[str, Any]:
    """Parse and validate a deep link URL, returning an action descriptor.

    This is the main entry point. It returns a dict with:
      - valid: bool
      - error: str (if invalid)
      - action: str (the route name)
      - target: str (the path component)
      - query: dict (query parameters)
      - raw_url: str

    The caller is responsible for executing the action.
    """
    parsed = parse_deep_link(url)
    if parsed is None:
        return {
            "valid": False,
            "error": f"URL does not start with {URL_SCHEME}://",
            "raw_url": url,
        }

    is_valid, error = validate_deep_link(parsed)
    if not is_valid:
        return {
            "valid": False,
            "error": error,
            "raw_url": url,
            **parsed.as_dict(),
        }

    return {
        "valid": True,
        "error": "",
        "action": parsed.route,
        "target": parsed.path,
        "query": parsed.query,
        "raw_url": url,
    }


def format_help() -> str:
    """Return a help string listing all supported deep link routes."""
    lines = [f"Algo CLI URL scheme: {URL_SCHEME}://<route>/<path>?<query>"]
    lines.append("")
    lines.append("Supported routes:")
    for route, info in sorted(KNOWN_ROUTES.items()):
        path_req = " <path>" if info["path_required"] else ""
        lines.append(f"  {URL_SCHEME}://{route}{path_req}")
        lines.append(f"    {info['description']}")
    lines.append("")
    lines.append("Examples:")
    lines.append(f"  {URL_SCHEME}://skill/echo-veil-integration")
    lines.append(f"  {URL_SCHEME}://memory/recall?q=rebrand")
    lines.append(f"  {URL_SCHEME}://session/new")
    lines.append(f"  {URL_SCHEME}://session/load?name=my-session")
    lines.append(f"  {URL_SCHEME}://version")
    return "\n".join(lines)