"""Focused terminal setup for provider credentials and OAuth flows.

Keeping credentials out of the chat REPL makes the normal slash palette about
session work instead of account administration.  The commands intentionally
never print credential values and only contact a provider after the user asks
to log in or verify it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .config import load_runtime_env, runtime_env_path, update_runtime_env
from .display import console


_PROVIDERS = ("google", "xai", "chatgpt", "ollama")
_SETUP_CHOICES = {
    "1": "google",
    "2": "xai",
    "3": "chatgpt",
    "4": "ollama",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="algo-cli config",
        description="Set up Algo CLI providers without filling the interactive slash palette.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show safe provider readiness (credential values stay redacted).")

    setup = subparsers.add_parser("setup", help="Run the guided setup for one provider.")
    setup.add_argument("provider", nargs="?", choices=_PROVIDERS)
    setup.add_argument("provider_args", nargs=argparse.REMAINDER)

    auth = subparsers.add_parser("auth", help="Log in, log out, verify, or inspect a configured provider.")
    auth.add_argument("provider", choices=_PROVIDERS)
    auth.add_argument("action", nargs="?", default="status")
    auth.add_argument("provider_args", nargs=argparse.REMAINDER)

    env = subparsers.add_parser("env", help="Inspect the selected local runtime-env file.")
    env.add_argument("action", nargs="?", default="path", choices=("path",))
    return parser


def _provider_main() -> Any:
    """Import command handlers lazily to avoid a module import cycle."""

    from . import main

    return main


def _prompt(
    prompt: str,
    *,
    input_fn: Callable[[str], str],
    secret: bool = False,
    secret_input: Callable[[str], str],
) -> str | None:
    try:
        value = secret_input(prompt) if secret else input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]Setup cancelled.[/]")
        return None
    return str(value).strip()


def _provider_status() -> None:
    """Print a no-secrets provider summary suitable for support transcripts."""

    from . import chatgpt_auth, google_workspace_auth, xai_auth

    load_runtime_env(override=True)
    xai = xai_auth.auth_status()
    google = google_workspace_auth.auth_status()
    chatgpt = chatgpt_auth.auth_status()
    ollama_ready = bool(os.environ.get("OLLAMA_API_KEY", "").strip())

    if xai.get("api_key_configured"):
        xai_line = "configured (API key redacted)"
    else:
        xai_line = "not configured — run `algo-cli config setup xai`"
    if xai.get("legacy_oauth_detected"):
        xai_line += "; legacy xAI OAuth data is ignored"

    if google.get("authenticated"):
        google_line = "authenticated"
    elif google.get("client_configured"):
        google_line = "client configured; login required"
    elif google.get("token_present"):
        google_line = "stored token cannot refresh without a client configuration"
    else:
        google_line = "not configured — run `algo-cli config setup google`"

    if chatgpt.get("authenticated"):
        chatgpt_line = "authenticated"
    else:
        chatgpt_line = "not authenticated — run `algo-cli config setup chatgpt`"

    ollama_line = "direct API key configured (redacted)" if ollama_ready else "no direct API key configured"

    console.print("[bold]Algo CLI provider setup[/]")
    console.print(f"  xAI API       {xai_line}")
    console.print(f"  Google        {google_line}")
    console.print(f"  ChatGPT/Codex {chatgpt_line}")
    console.print(f"  Ollama Cloud  {ollama_line}")
    console.print("[dim]Use `algo-cli config setup PROVIDER` to change one provider. Values are never displayed.[/]")


def _setup_xai(
    *,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> int:
    from . import xai_auth

    console.print(
        "[bold]xAI API setup[/]\n"
        "xAI documents API-key authentication for api.x.ai. A key is only used after you explicitly select a "
        "grok-* model or run an xAI action; those calls may consume paid API usage."
    )
    key = _prompt(
        "xAI API key (input hidden; blank cancels): ",
        input_fn=input_fn,
        secret=True,
        secret_input=secret_input,
    )
    if not key:
        console.print("[dim]xAI setup unchanged.[/]")
        return 2
    try:
        update_runtime_env({xai_auth.XAI_API_KEY_ENV: key})
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not save xAI configuration: {exc}[/]")
        return 1
    console.print(
        "[green]xAI API key saved locally (redacted).[/] "
        "Use `algo-cli config auth xai verify` when you are ready to make a read-only model-list request."
    )
    return 0


def _setup_google(
    provider_args: Sequence[str],
    *,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> int:
    load_runtime_env(override=True)
    existing_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    existing_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    console.print(
        "[bold]Google Workspace setup[/]\n"
        "Create a Google OAuth client of type Desktop app, enable only the Workspace APIs you need, and use its "
        "client ID here. Desktop clients use a local 127.0.0.1 callback and normally do not need a client secret."
    )
    client_id = existing_id or _prompt(
        "Google OAuth client ID: ",
        input_fn=input_fn,
        secret=False,
        secret_input=secret_input,
    )
    if not client_id:
        console.print("[dim]Google setup unchanged.[/]")
        return 2
    updates: dict[str, str | None] = {"GOOGLE_OAUTH_CLIENT_ID": client_id}
    if not existing_secret:
        secret = _prompt(
            "Google OAuth client secret (leave blank for a Desktop app): ",
            input_fn=input_fn,
            secret=True,
            secret_input=secret_input,
        )
        if secret:
            updates["GOOGLE_OAUTH_CLIENT_SECRET"] = secret
    try:
        update_runtime_env(updates)
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not save Google configuration: {exc}[/]")
        return 1
    # Reload values to cover an explicit env-file and immediately start the
    # same secure PKCE loopback flow the runtime uses.
    load_runtime_env(override=True)
    return 0 if _provider_main().run_google_login(" ".join(provider_args)) else 1


def _setup_chatgpt(provider_args: Sequence[str]) -> int:
    console.print("[bold]ChatGPT/Codex setup[/]")
    return 0 if _provider_main().run_chatgpt_login(" ".join(provider_args)) else 1


def _setup_ollama(
    *,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> int:
    console.print(
        "[bold]Ollama Cloud setup[/]\n"
        "A direct Cloud API key is optional. Local Ollama sign-in can still run :cloud models without storing it."
    )
    key = _prompt(
        "Ollama API key (input hidden; blank cancels): ",
        input_fn=input_fn,
        secret=True,
        secret_input=secret_input,
    )
    if not key:
        console.print("[dim]Ollama Cloud setup unchanged.[/]")
        return 2
    try:
        update_runtime_env({"OLLAMA_API_KEY": key})
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not save Ollama configuration: {exc}[/]")
        return 1
    console.print("[green]Ollama API key saved locally (redacted).[/]")
    return 0


def _interactive_setup_choice(
    *,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
) -> str | None:
    console.print("[bold]Choose a provider to set up[/]")
    console.print("  1. Google Workspace OAuth")
    console.print("  2. xAI API key")
    console.print("  3. ChatGPT/Codex OAuth")
    console.print("  4. Ollama Cloud")
    value = _prompt(
        "Select [1-4, blank to cancel]: ",
        input_fn=input_fn,
        secret=False,
        secret_input=secret_input,
    )
    if not value:
        return None
    provider = _SETUP_CHOICES.get(value.strip().lower(), value.strip().lower())
    if provider not in _PROVIDERS:
        console.print("[red]Choose 1-4 or a provider name (google, xai, chatgpt, ollama).[/]")
        return None
    return provider


def _run_setup(
    provider: str | None,
    provider_args: Sequence[str],
    *,
    input_fn: Callable[[str], str],
    secret_input: Callable[[str], str],
    interactive: bool,
) -> int:
    if provider is None:
        if not interactive:
            console.print("[yellow]Choose a provider: `algo-cli config setup google|xai|chatgpt|ollama`.[/]")
            return 2
        provider = _interactive_setup_choice(input_fn=input_fn, secret_input=secret_input)
        if provider is None:
            return 2
    if provider == "xai":
        return _setup_xai(input_fn=input_fn, secret_input=secret_input)
    if provider == "google":
        return _setup_google(provider_args, input_fn=input_fn, secret_input=secret_input)
    if provider == "chatgpt":
        return _setup_chatgpt(provider_args)
    if provider == "ollama":
        return _setup_ollama(input_fn=input_fn, secret_input=secret_input)
    console.print(f"[red]Unknown provider: {provider}[/]")
    return 2


def _remove_xai_key() -> int:
    from . import xai_auth

    try:
        update_runtime_env({xai_auth.XAI_API_KEY_ENV: None})
    except (RuntimeError, ValueError) as exc:
        console.print(f"[red]Could not remove xAI configuration: {exc}[/]")
        return 1
    console.print("[green]Saved xAI API key removed from the runtime env file.[/]")
    return 0


def _run_auth(provider: str, action: str, provider_args: Sequence[str], *, input_fn: Callable[[str], str], secret_input: Callable[[str], str], interactive: bool) -> int:
    normalized = action.strip().lower() or "status"
    if normalized in {"setup", "configure"}:
        return _run_setup(
            provider,
            provider_args,
            input_fn=input_fn,
            secret_input=secret_input,
            interactive=interactive,
        )
    if normalized == "status":
        _provider_status()
        return 0

    main = _provider_main()
    args = " ".join(provider_args)
    if provider == "xai":
        if normalized in {"login", "signin"}:
            console.print("[yellow]xAI API access uses an API key, not a consumer OAuth login.[/]")
            return _run_setup("xai", provider_args, input_fn=input_fn, secret_input=secret_input, interactive=interactive)
        if normalized in {"verify", "test"}:
            return 0 if main.run_xai_test() else 1
        if normalized in {"logout", "remove", "clear"}:
            return _remove_xai_key()
    elif provider == "google":
        if normalized in {"login", "signin"}:
            return 0 if main.run_google_login(args) else 1
        if normalized in {"logout", "remove", "clear"}:
            return 0 if main.run_google_logout() else 1
    elif provider == "chatgpt":
        if normalized in {"login", "signin"}:
            return 0 if main.run_chatgpt_login(args) else 1
        if normalized in {"logout", "remove", "clear"}:
            return 0 if main.run_chatgpt_logout() else 1
    elif provider == "ollama":
        if normalized in {"login", "signin"}:
            return 0 if main.run_ollama_login() else 1
        if normalized in {"logout", "remove", "clear"}:
            try:
                update_runtime_env({"OLLAMA_API_KEY": None})
            except (RuntimeError, ValueError) as exc:
                console.print(f"[red]Could not remove Ollama configuration: {exc}[/]")
                return 1
            console.print("[green]Saved Ollama API key removed from the runtime env file.[/]")
            return 0

    console.print(
        f"[yellow]Unsupported action for {provider}: {action}. "
        "Use status, setup, login, logout, or verify (xAI).[/]"
    )
    return 2


def run(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass.getpass,
    interactive: bool | None = None,
) -> int:
    """Run ``algo-cli config`` and return a shell-style status code."""

    parser = build_parser()
    try:
        namespace = parser.parse_args(list(argv or ()))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 2
    is_interactive = sys.stdin.isatty() if interactive is None else interactive

    if namespace.command in {None, "status"}:
        _provider_status()
        if namespace.command is None:
            console.print("[dim]Start guided setup with `algo-cli config setup PROVIDER`.[/]")
        return 0
    if namespace.command == "setup":
        return _run_setup(
            namespace.provider,
            namespace.provider_args,
            input_fn=input_fn,
            secret_input=secret_input,
            interactive=is_interactive,
        )
    if namespace.command == "auth":
        return _run_auth(
            namespace.provider,
            namespace.action,
            namespace.provider_args,
            input_fn=input_fn,
            secret_input=secret_input,
            interactive=is_interactive,
        )
    if namespace.command == "env":
        console.print(str(runtime_env_path()))
        return 0
    parser.error(f"Unsupported config command: {namespace.command}")
    return 2
