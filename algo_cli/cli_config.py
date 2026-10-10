"""Focused terminal setup for provider credentials and OAuth flows.

Keeping credentials out of the chat REPL makes the normal slash palette about
session work instead of account administration.  The commands intentionally
never print credential values and only contact a provider after the user asks
to log in or verify it.
"""

from __future__ import annotations

import argparse
from difflib import get_close_matches
import getpass
import json
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
    setup.add_argument("provider", nargs="?")
    setup.add_argument("provider_args", nargs=argparse.REMAINDER)

    auth = subparsers.add_parser("auth", help="Log in, log out, verify, or inspect a configured provider.")
    auth.add_argument("provider")
    auth.add_argument("action", nargs="?", default="status")
    auth.add_argument("provider_args", nargs=argparse.REMAINDER)

    env = subparsers.add_parser("env", help="Inspect the selected local runtime-env file.")
    env.add_argument("action", nargs="?", default="path", choices=("path",))
    memory = subparsers.add_parser(
        "memory",
        help="Repair Continuum selection or provision and inspect protected receipts.",
    )
    memory.add_argument("action", choices=("status", "provision", "repair", "repair-goal"))
    jev = subparsers.add_parser("jev", help="Configure the advisory Jev question-contract companion.")
    jev.add_argument("action", choices=("status", "enable", "disable"))
    jev.add_argument("--cli", help="Absolute path to the installed jev-workflows executable.")
    decision = subparsers.add_parser("decision", help="Configure bounded decision providers and explicit routing.")
    decision.add_argument("action", choices=("status", "select", "configure", "route", "fallback", "coordinate"), nargs="?", default="status")
    decision.add_argument("provider", nargs="?")
    decision.add_argument("--model")
    decision.add_argument("--model-digest")
    decision.add_argument("--base-url")
    decision.add_argument("--cli")
    decision.add_argument("--task")
    decision.add_argument("--fallback", nargs="*")
    decision.add_argument("--descriptor", help="Reviewed extra Ollama System One model descriptor JSON.")
    decision.add_argument("--timeout", type=float)
    decision.add_argument("--keep-alive")
    decision.add_argument("--candidates", nargs="+")
    decision.add_argument("--offline-coordinators", nargs="+")
    decision.add_argument("--minimum-probability", type=float)
    decision.add_argument("--minimum-margin", type=float)
    decision.add_argument("--minimum-fit", type=float)
    return parser


def _provider_main() -> Any:
    """Import command handlers lazily to avoid a module import cycle."""

    from . import main

    return main


def _unknown_provider(provider: str) -> int:
    normalized = str(provider or "").strip().lower()
    matches = get_close_matches(normalized, _PROVIDERS, n=1, cutoff=0.55)
    suggestion = f" Did you mean `{matches[0]}`?" if matches else ""
    console.print(
        f"[red]Unknown provider: {provider}.[/]"
        f"{suggestion} Choose google, xai, chatgpt, or ollama."
    )
    return 2


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
    provider = provider.strip().lower()
    if provider == "xai":
        return _setup_xai(input_fn=input_fn, secret_input=secret_input)
    if provider == "google":
        return _setup_google(provider_args, input_fn=input_fn, secret_input=secret_input)
    if provider == "chatgpt":
        return _setup_chatgpt(provider_args)
    if provider == "ollama":
        return _setup_ollama(input_fn=input_fn, secret_input=secret_input)
    return _unknown_provider(provider)


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
    provider = provider.strip().lower()
    if provider not in _PROVIDERS:
        return _unknown_provider(provider)
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


def _run_memory(action: str) -> int:
    if action == "repair":
        from .config import repair_memory_configuration

        try:
            result = repair_memory_configuration()
        except (OSError, RuntimeError):
            console.print(
                "[red]Memory configuration repair failed safely. The original "
                "configuration and any existing backup were not replaced.[/]"
            )
            return 1
        state = "repaired" if result["changed"] else "already selected"
        console.print(f"Native Continuum memory is {state}.")
        console.print(f"Previous configuration retained at {result['backup_path']}.")
        return 0

    if action == "repair-goal":
        from .ada_task_ledger import repair_protected_goal_store_conflict

        try:
            result = repair_protected_goal_store_conflict()
        except (OSError, RuntimeError):
            console.print(
                "[red]Protected goal repair failed safely. The trusted anchor "
                "and existing goal state were not reset or discarded.[/]"
            )
            return 1
        if result["changed"]:
            console.print("Protected goal conflict repaired as blocked, non-resumable state.")
            console.print(f"Exact legacy ledger retained at {result['backup_path']}.")
        else:
            console.print("Protected goal state is already consistent.")
        return 0

    from .elsie_keyring_anchors import ElsieKeyringAnchorStore
    from .grace_key_store import KeyringKeyStore
    from .irene_privacy_views import PRIVACY_KEY_LABEL

    try:
        store = KeyringKeyStore()
        anchors = ElsieKeyringAnchorStore(store)
        status = anchors.status()
        if action == "provision":
            # Validate existing state before creating a key. Missing authority
            # for an existing bundle must never become an implicit key reset.
            store.get_or_create(PRIVACY_KEY_LABEL, length=32)
            anchors.provision()
            status = anchors.status()
        ready = status["provisioned"]
        console.print(
            "CLI memory receipts: " + ("OS-protected and provisioned." if ready else "not provisioned.")
        )
        console.print("Native browser authority is unchanged; its signed installation gates still apply.")
        return 0 if ready else 1
    except Exception:
        console.print(
            "[red]CLI memory credential preparation failed safely. "
            "Existing keys and invalid credentials are not reset; no plaintext fallback was enabled.[/]"
        )
        return 1


def _run_jev(action: str, cli: str | None) -> int:
    from pathlib import Path

    from .config import Config
    from .jev_kernel import kernel_status

    cfg = Config.load()
    if cli is not None:
        if action != "enable" or not Path(cli).expanduser().is_absolute():
            console.print("Jev setup requires enable --cli with an absolute executable path.")
            return 2
        cfg.jev_kernel_cli = str(Path(cli).expanduser().resolve())
    if action != "disable":
        result = kernel_status(cfg)
        console.print(json.dumps(result), markup=False)
        if not result["ok"]:
            return 1
    if action in {"enable", "disable"}:
        cfg.jev_kernel_enabled = action == "enable"
        try:
            cfg.save()
        except (OSError, RuntimeError):
            console.print("Jev setup could not be saved; repair the configuration before retrying.")
            return 1
        console.print(
            "Jev enabled: explicit question packets may be sent to TypeSafe under normal tool policy."
            if cfg.jev_kernel_enabled else "Jev inference disabled; local lint remains available."
        )
    return 0


def _run_decision(namespace: Any) -> int:
    from pathlib import Path
    from .config import Config
    from .decision_layer import DecisionError, ID_RE, decision_status, provider_for, selected_providers

    cfg = Config.load()
    if namespace.action == "status":
        print(decision_status(namespace.provider, cfg))
        return 0
    try:
        name = namespace.provider
        if type(name) is not str or not ID_RE.fullmatch(name):
            raise DecisionError("invalid_provider")
        if namespace.action == "coordinate":
            from .decision_coordinator import _settings
            if name not in {"jev", "direct"}:
                raise DecisionError("invalid_coordinator")
            cfg.decision_routing_mode = name
            if name == "jev":
                cfg.decision_default_provider = "jev"
            for attribute, value in (
                ("decision_router_candidates", namespace.candidates),
                ("decision_offline_coordinators", namespace.offline_coordinators),
                ("decision_router_min_probability", namespace.minimum_probability),
                ("decision_router_min_margin", namespace.minimum_margin),
                ("decision_router_min_fit", namespace.minimum_fit),
            ):
                if value is not None:
                    setattr(cfg, attribute, value)
            names, _profiles, _policy = _settings(cfg)
            for candidate in names:
                provider_for(candidate, cfg)
            if (not cfg.decision_offline_coordinators or len(cfg.decision_offline_coordinators) > 2
                    or len(set(cfg.decision_offline_coordinators)) != len(cfg.decision_offline_coordinators)
                    or any(p not in {"clef", "clef-flash", "tev1"} or p not in names for p in cfg.decision_offline_coordinators)):
                raise DecisionError("invalid_offline_coordinators")
        elif namespace.action == "configure":
            settings = dict(cfg.decision_provider_settings.get(name, {}))
            if namespace.descriptor:
                payload = Path(namespace.descriptor).expanduser().read_bytes()
                if len(payload) > 16_000:
                    raise DecisionError("descriptor_too_large")
                settings = json.loads(payload)
                if type(settings) is not dict:
                    raise DecisionError("invalid_descriptor")
            for key, value in (("model", namespace.model), ("model_digest", namespace.model_digest),
                               ("base_url", namespace.base_url), ("cli", namespace.cli)):
                if value is not None:
                    settings[key] = str(Path(value).expanduser().resolve()) if key == "cli" else value
            cfg.decision_provider_settings[name] = settings
        if namespace.action != "coordinate":
            provider_for(name, cfg)
        if namespace.action == "select":
            cfg.decision_default_provider = name
            cfg.decision_routing_mode = "direct"
        elif namespace.action == "route":
            if type(namespace.task) is not str or not ID_RE.fullmatch(namespace.task):
                raise DecisionError("invalid_task")
            cfg.decision_routes[namespace.task] = name
        elif namespace.action == "fallback":
            if namespace.fallback is None:
                raise DecisionError("explicit_fallback_list_required")
            for fallback in namespace.fallback:
                provider_for(fallback, cfg)
            cfg.decision_fallbacks[name] = namespace.fallback
            selected_providers(cfg, name, "")
        if namespace.timeout is not None:
            if not 1 <= namespace.timeout <= 300:
                raise DecisionError("invalid_timeout")
            cfg.decision_timeout_seconds = namespace.timeout
        if namespace.keep_alive is not None:
            cfg.decision_keep_alive = namespace.keep_alive
        _save_decision_configuration(namespace, cfg)
        console.print(f"Decision configuration saved; default provider is {cfg.decision_default_provider}.")
        return 0
    except (DecisionError, OSError, ValueError, TypeError):
        console.print("[error]Invalid or unavailable decision configuration; no change was saved.[/]")
        return 2


def _save_decision_configuration(namespace: Any, cfg: Any) -> None:
    from . import config as storage
    from .decision_layer import DecisionError, decode

    if cfg.memory_config_error:
        raise DecisionError("memory_configuration_requires_repair")
    storage._ensure_private_config_parent(storage.CONFIG_FILE)
    with storage._exclusive_state_lock(storage.CONFIG_FILE):
        try:
            current = decode(storage._state_descriptor_payload(storage.CONFIG_FILE, max_bytes=storage.MAX_JSON_STATE_BYTES))
        except FileNotFoundError:
            current = {}
        name = namespace.provider
        if namespace.action == "configure":
            existing = current.setdefault("decision_provider_settings", {})
            if type(existing) is not dict:
                raise DecisionError("invalid_provider_configuration")
            if namespace.descriptor:
                existing[name] = cfg.decision_provider_settings[name]
            else:
                row = existing.setdefault(name, {})
                if type(row) is not dict:
                    raise DecisionError("invalid_provider_configuration")
                for key, value in (("model", namespace.model), ("model_digest", namespace.model_digest),
                                   ("base_url", namespace.base_url), ("cli", namespace.cli)):
                    if value is not None:
                        row[key] = cfg.decision_provider_settings[name][key]
        elif namespace.action == "select":
            current["decision_default_provider"] = name
            current["decision_routing_mode"] = "direct"
        elif namespace.action == "coordinate":
            for key in ("decision_routing_mode", "decision_default_provider", "decision_router_candidates",
                        "decision_offline_coordinators", "decision_router_min_probability",
                        "decision_router_min_margin", "decision_router_min_fit"):
                current[key] = getattr(cfg, key)
        elif namespace.action in {"route", "fallback"}:
            field = "decision_routes" if namespace.action == "route" else "decision_fallbacks"
            mapping = current.setdefault(field, {})
            if type(mapping) is not dict:
                raise DecisionError("invalid_routing_configuration")
            key = namespace.task if namespace.action == "route" else name
            mapping[key] = getattr(cfg, field)[key]
        if namespace.timeout is not None:
            current["decision_timeout_seconds"] = cfg.decision_timeout_seconds
        if namespace.keep_alive is not None:
            current["decision_keep_alive"] = cfg.decision_keep_alive
        storage._atomic_write_text(storage.CONFIG_FILE, json.dumps(current, indent=2))


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
    if namespace.command == "memory":
        return _run_memory(namespace.action)
    if namespace.command == "jev":
        return _run_jev(namespace.action, namespace.cli)
    if namespace.command == "decision":
        return _run_decision(namespace)
    parser.error(f"Unsupported config command: {namespace.command}")
    return 2
