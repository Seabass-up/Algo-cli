"""B60. LSP Integration for Code Intelligence.

Language Server Protocol integration for go-to-definition, hover,
diagnostics, and references.  Source: copilot-cli pattern.
"""
from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any


class LSPServerStatus(Enum):
    STOPPED = auto()
    STARTING = auto()
    READY = auto()
    ERROR = auto()


@dataclass
class LSPDiagnostic:
    severity: str  # "error", "warning", "info", "hint"
    message: str
    line: int  # 0-based
    col: int
    end_line: int = 0
    end_col: int = 0
    source: str = ""
    code: str = ""


@dataclass
class LSPDefinition:
    file: str
    line: int
    col: int


@dataclass
class LSPHover:
    contents: str
    range_line: int = 0


@dataclass
class LSPServer:
    name: str
    command: list[str]
    languages: list[str]
    status: LSPServerStatus = LSPServerStatus.STOPPED
    process: Any = None


class LSPManager:
    """Manage LSP servers and provide code intelligence."""

    # Common LSP server configs
    SERVER_CONFIGS: dict[str, LSPServer] = {
        "pyright": LSPServer(
            name="pyright",
            command=["pyright-langserver", "--stdio"],
            languages=["python"],
        ),
        "pylsp": LSPServer(
            name="pylsp",
            command=["pylsp"],
            languages=["python"],
        ),
        "typescript": LSPServer(
            name="typescript-language-server",
            command=["typescript-language-server", "--stdio"],
            languages=["typescript", "javascript"],
        ),
        "rust-analyzer": LSPServer(
            name="rust-analyzer",
            command=["rust-analyzer"],
            languages=["rust"],
        ),
    }

    def __init__(self) -> None:
        self._servers: dict[str, LSPServer] = {}
        self._file_to_server: dict[str, str] = {}

    def register_server(self, server: LSPServer) -> None:
        self._servers[server.name] = server
        for lang in server.languages:
            self._file_to_server[lang] = server.name

    def get_server_for_file(self, file_path: str) -> LSPServer | None:
        ext = Path(file_path).suffix.lower()
        lang_map = {
            ".py": "python",
            ".ts": "typescript",
            ".js": "javascript",
            ".rs": "rust",
        }
        lang = lang_map.get(ext)
        if not lang:
            return None
        server_name = self._file_to_server.get(lang)
        if not server_name:
            return None
        return self._servers.get(server_name)

    def parse_diagnostics(self, output: str) -> list[LSPDiagnostic]:
        """Parse LSP diagnostic output."""
        diags: list[LSPDiagnostic] = []
        try:
            data = json.loads(output)
            for item in data.get("diagnostics", []):
                diags.append(LSPDiagnostic(
                    severity=item.get("severity", "info"),
                    message=item.get("message", ""),
                    line=item.get("range", {}).get("start", {}).get("line", 0),
                    col=item.get("range", {}).get("start", {}).get("character", 0),
                    end_line=item.get("range", {}).get("end", {}).get("line", 0),
                    end_col=item.get("range", {}).get("end", {}).get("character", 0),
                    source=item.get("source", ""),
                    code=item.get("code", ""),
                ))
        except (json.JSONDecodeError, KeyError):
            pass
        return diags

    def check_available(self) -> dict[str, bool]:
        """Check which LSP servers are installed."""
        available: dict[str, bool] = {}
        for name, server in self.SERVER_CONFIGS.items():
            try:
                cmd = server.command[0]
                result = subprocess.run(
                    ["where" if _is_windows() else "which", cmd],
                    capture_output=True, timeout=5,
                )
                available[name] = result.returncode == 0
            except Exception:
                available[name] = False
        return available


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"