"""Ollama / xAI clients, local server startup, and harness-gateway lifecycle."""

from __future__ import annotations

import atexit
import ipaddress
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from ollama import Client

from .config import Config, load_runtime_env
from . import harness
from . import model_info as _model_info_module
from . import xai_auth
from . import chatgpt_auth
from .display import show_error, show_info
from .model_routing import require_cloud_api_key, uses_ollama_cloud

LOCAL_STARTUP_TIMEOUT_SECONDS = 12
SERVER_READY_TTL_SECONDS = 5.0
SERVER_NOT_READY_TTL_SECONDS = 0.25
GATEWAY_READY_TTL_SECONDS = 5.0

SERVER_READY_CACHE: dict[str, tuple[float, bool]] = {}
_SERVER_READY_CACHE_LOCK = threading.Lock()
GATEWAY_PROCESS: subprocess.Popen[Any] | None = None
GATEWAY_READY_CACHE: dict[str, tuple[float, bool]] = {}
_TOOL_ENV_KEYS = ("OLLAMA_HOST", "ALGO_CLI_GATEWAY_URL", "OLLAMA_CLI_GATEWAY_URL")
_TOOL_ENV_CONDITION = threading.Condition(threading.RLock())
_TOOL_ENV_ACTIVE_VALUES: tuple[str, str, str] | None = None
_TOOL_ENV_ACTIVE_COUNT = 0
_TOOL_ENV_PREVIOUS: dict[str, str | None] = {}
_TOOL_ENV_THREAD_STATE = threading.local()


def create_client(cfg: Config) -> Any:
    load_runtime_env(override=True)
    if _model_info_module.is_xai_model(cfg.model):
        from . import xai_client

        return xai_client.active_xai_client()
    if _model_info_module.is_chatgpt_model(cfg.model):
        from . import chatgpt_client

        return chatgpt_client.active_chatgpt_client()
    timeout = max(1.0, float(cfg.chat_stream_timeout_seconds))
    if uses_ollama_cloud(cfg):
        require_cloud_api_key(cfg)
        api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
        headers = {"Authorization": f"Bearer {api_key}"}
        return Client(host="https://ollama.com", headers=headers, timeout=timeout)
    return Client(host=cfg.host, timeout=timeout)


def client_for_model(model: str, cfg: Config, active_client: Any) -> Any:
    if not model or model == cfg.model:
        return active_client
    load_runtime_env(override=True)
    if _model_info_module.is_xai_model(model):
        if not xai_auth.get_valid_token():
            show_info(
                f"Agent block model {model} needs XAI_API_KEY; falling back to {cfg.model}. "
                "Run `algo-cli config setup xai` to configure it."
            )
            return active_client
        from . import xai_client

        return xai_client.active_xai_client()
    if _model_info_module.is_chatgpt_model(model):
        if not chatgpt_auth.get_valid_token():
            show_info(f"Agent block model {model} needs ChatGPT OAuth; falling back to {cfg.model}.")
            return active_client
        from . import chatgpt_client

        return chatgpt_client.active_chatgpt_client()
    timeout = max(1.0, float(cfg.chat_stream_timeout_seconds))
    if uses_ollama_cloud(cfg):
        require_cloud_api_key(cfg)
        api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
        headers = {"Authorization": f"Bearer {api_key}"}
        return Client(host="https://ollama.com", headers=headers, timeout=timeout)
    return Client(host=cfg.host, timeout=timeout)


def apply_tool_runtime_env(cfg: Config) -> None:
    os.environ["OLLAMA_HOST"] = cfg.host
    url = gateway_url()
    os.environ["ALGO_CLI_GATEWAY_URL"] = url
    os.environ["OLLAMA_CLI_GATEWAY_URL"] = url
    if cfg.cloud:
        load_runtime_env(override=True)


@contextmanager
def scoped_tool_runtime_env(cfg: Config):
    """Lease process-global tool environment safely across agent threads.

    Equal configurations share a reference-counted lease and remain concurrent.
    Different configurations serialize because ``os.environ`` cannot represent
    both values at once.
    """
    global _TOOL_ENV_ACTIVE_VALUES, _TOOL_ENV_ACTIVE_COUNT, _TOOL_ENV_PREVIOUS

    url = gateway_url()
    desired = (str(cfg.host), url, url)
    thread_depth = int(getattr(_TOOL_ENV_THREAD_STATE, "depth", 0))
    with _TOOL_ENV_CONDITION:
        if thread_depth and _TOOL_ENV_ACTIVE_VALUES != desired:
            raise RuntimeError("nested tool runtime environments must use the same configuration")
        while _TOOL_ENV_ACTIVE_COUNT and _TOOL_ENV_ACTIVE_VALUES != desired:
            _TOOL_ENV_CONDITION.wait()
        if _TOOL_ENV_ACTIVE_COUNT == 0:
            _TOOL_ENV_PREVIOUS = {key: os.environ.get(key) for key in _TOOL_ENV_KEYS}
            try:
                apply_tool_runtime_env(cfg)
            except BaseException:
                for key, value in _TOOL_ENV_PREVIOUS.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                _TOOL_ENV_PREVIOUS = {}
                _TOOL_ENV_CONDITION.notify_all()
                raise
            _TOOL_ENV_ACTIVE_VALUES = desired
        _TOOL_ENV_ACTIVE_COUNT += 1
        _TOOL_ENV_THREAD_STATE.depth = thread_depth + 1
    try:
        yield
    finally:
        with _TOOL_ENV_CONDITION:
            _TOOL_ENV_THREAD_STATE.depth = max(0, int(getattr(_TOOL_ENV_THREAD_STATE, "depth", 1)) - 1)
            _TOOL_ENV_ACTIVE_COUNT -= 1
            if _TOOL_ENV_ACTIVE_COUNT == 0:
                for key, value in _TOOL_ENV_PREVIOUS.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                _TOOL_ENV_PREVIOUS = {}
                _TOOL_ENV_ACTIVE_VALUES = None
                _TOOL_ENV_CONDITION.notify_all()


def host_is_local(host: str) -> bool:
    """Return whether *host* resolves syntactically to a loopback endpoint.

    This intentionally avoids DNS resolution and substring checks. Remote
    names such as ``localhost.example`` must never trigger local process
    startup, while the full IPv4 loopback block remains valid.
    """

    value = str(host or "").strip()
    if not value:
        return False
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        hostname = parsed.hostname
    except ValueError:
        return False
    if not hostname:
        return False
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def ollama_server_ready(host: str) -> bool:
    now = time.monotonic()
    with _SERVER_READY_CACHE_LOCK:
        cached = SERVER_READY_CACHE.get(host)
    cache_ttl = SERVER_READY_TTL_SECONDS if cached and cached[1] else SERVER_NOT_READY_TTL_SECONDS
    if cached and now - cached[0] <= cache_ttl:
        return cached[1]
    try:
        request = Request(urljoin(host.rstrip("/") + "/", "api/version"), method="GET")
        with urlopen(request, timeout=1.5) as response:
            ready = 200 <= response.status < 500
    except (OSError, URLError, ValueError):
        ready = False
    with _SERVER_READY_CACHE_LOCK:
        SERVER_READY_CACHE[host] = (now, ready)
    return ready


def start_ollama_server(cfg: Config) -> bool:
    if (
        uses_ollama_cloud(cfg)
        or _model_info_module.is_xai_model(cfg.model)
        or _model_info_module.is_chatgpt_model(cfg.model)
        or not host_is_local(cfg.host)
    ):
        return True
    return start_local_ollama_host(cfg.host)


def start_local_ollama_host(host: str) -> bool:
    if not host_is_local(host):
        return True
    if ollama_server_ready(host):
        return True

    show_info("Local Ollama server is not responding; starting `ollama serve`.")
    try:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "start_new_session": True,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen(["ollama", "serve"], **kwargs)
        SERVER_READY_CACHE.pop(host, None)
    except FileNotFoundError:
        show_error("Could not find `ollama` on PATH. Install Ollama or add it to PATH.")
        return False
    except Exception as exc:
        show_error(f"Could not start Ollama server: {exc}")
        return False

    deadline = time.time() + LOCAL_STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        if ollama_server_ready(host):
            show_info("Local Ollama server is running.")
            return True
        time.sleep(0.5)

    show_error(f"Ollama server did not become ready at {host}.")
    return False


def gateway_url() -> str:
    return (
        os.environ.get("ALGO_CLI_GATEWAY_URL")
        or os.environ.get("OLLAMA_CLI_GATEWAY_URL")
        or "http://127.0.0.1:8765"
    ).rstrip("/")


def gateway_ready(url: str | None = None) -> bool:
    url = (url or gateway_url()).rstrip("/")
    cached = GATEWAY_READY_CACHE.get(url)
    now = time.time()
    if cached and now - cached[0] <= GATEWAY_READY_TTL_SECONDS:
        return cached[1]
    try:
        request = Request(url + "/healthz", method="GET")
        with urlopen(request, timeout=1.0) as response:
            ready = 200 <= response.status < 500
    except (OSError, URLError, ValueError):
        ready = False
    GATEWAY_READY_CACHE[url] = (now, ready)
    return ready


def gateway_source_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "harness-gateway"


def gateway_command() -> tuple[list[str], Path] | None:
    configured = os.environ.get("ALGO_CLI_GATEWAY_BIN") or os.environ.get("OLLAMA_CLI_GATEWAY_BIN")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return [str(path)], path.parent
    source_dir = gateway_source_dir()
    exe_name = "harness-gateway.exe" if sys.platform.startswith("win") else "harness-gateway"
    built = source_dir / exe_name
    if built.exists():
        return [str(built)], source_dir
    go = shutil.which("go") or (r"C:\Program Files\Go\bin\go.exe" if Path(r"C:\Program Files\Go\bin\go.exe").exists() else "")
    if go and source_dir.exists():
        return [go, "run", "."], source_dir
    return None


def start_supplemental_gateway(cfg: Config) -> bool:
    global GATEWAY_PROCESS
    if not uses_ollama_cloud(cfg):
        return True
    if not start_local_ollama_host(cfg.host):
        return False
    url = gateway_url()
    if gateway_ready(url):
        return True
    command_info = gateway_command()
    if command_info is None:
        show_info(
            "Supplemental gateway unavailable. Build `harness-gateway` or install Go "
            "for local embedding/OCR helpers in cloud mode."
        )
        return False
    command, cwd = command_info
    startup_timeout = 45 if len(command) >= 2 and Path(command[0]).name.lower().startswith("go") and command[1] == "run" else 20
    addr = url.removeprefix("http://").removeprefix("https://")
    args = command + ["-addr", addr, "-index", str(harness.INDEX_PATH), "-ollama", cfg.host]
    try:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        GATEWAY_PROCESS = subprocess.Popen(args, **kwargs)
        GATEWAY_READY_CACHE.pop(url, None)
    except Exception as exc:
        show_error(f"Could not start supplemental gateway: {exc}")
        return False
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if gateway_ready(url):
            show_info(f"Supplemental gateway is running at {url}.")
            return True
        time.sleep(0.5)
    show_error(f"Supplemental gateway did not become ready at {url}.")
    return False


def shutdown_supplemental_gateway() -> None:
    global GATEWAY_PROCESS
    if GATEWAY_PROCESS and GATEWAY_PROCESS.poll() is None:
        try:
            GATEWAY_PROCESS.terminate()
            GATEWAY_PROCESS.wait(timeout=3)
        except Exception:
            try:
                GATEWAY_PROCESS.kill()
            except Exception:
                pass
    GATEWAY_PROCESS = None


atexit.register(shutdown_supplemental_gateway)
