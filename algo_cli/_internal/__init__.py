"""algo_cli/_internal — Internal helpers and runtime modules.

This subpackage is for code that the user does not import directly. It is
imported by algo_cli and its public API. Importing from this package
directly may emit a DeprecationWarning in a future version.

Modules:
- policy_chain: J10 — PAM-style policy chain (required/sufficient/requisite/include)
"""
from __future__ import annotations

__all__: list[str] = []
