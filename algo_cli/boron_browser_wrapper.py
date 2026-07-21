"""Finite Chromium pipe wrapper for the disabled managed-browser foundation.

The wrapper is intended to run *inside* the Boron browser container.  It never
opens a DevTools TCP port and never accepts caller-supplied CDP methods, Chrome
flags, JavaScript, selectors, or filesystem paths.  Chromium speaks ASCIIZ JSON
on inherited file descriptors 3 and 4; this module bounds and validates that
transport and reduces it to one navigation lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, NoReturn, Sequence
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes


BORON_PIPE_PROTOCOL_VERSION = 1
BORON_MAX_PIPE_MESSAGE_BYTES = 1_048_576
BORON_MAX_PIPE_BUFFER_BYTES = 2 * BORON_MAX_PIPE_MESSAGE_BYTES
BORON_MAX_JSON_DEPTH = 8
BORON_MAX_JSON_ITEMS = 512
BORON_MAX_STRING_BYTES = 65_536
BORON_MAX_NAVIGATION_MS = 120_000
BORON_CHROME_PATH = "/opt/algo/chrome/chrome"
BORON_CERTUTIL_PATH = "/usr/bin/certutil"
BORON_PROFILE_PATH = Path("/algo-profile")
BORON_CA_PATH = BORON_PROFILE_PATH / "xenon-session-ca.pem"

_VERSION_RE = re.compile(r"^[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){3}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

_ALLOWED_METHODS = frozenset(
    {
        "Browser.getVersion",
        "Browser.setDownloadBehavior",
        "Network.enable",
        "Network.setBlockedURLs",
        "Page.enable",
        "Page.navigate",
        "Page.setInterceptFileChooserDialog",
        "Page.setLifecycleEventsEnabled",
        "Page.stopLoading",
        "Target.attachToTarget",
        "Target.createBrowserContext",
        "Target.createTarget",
        "Target.disposeBrowserContext",
        "Target.setAutoAttach",
    }
)

_BOOTSTRAP_PAGE_METHODS = (
    "Page.enable",
    "Network.enable",
    "Network.setBlockedURLs",
    "Page.setLifecycleEventsEnabled",
    "Page.setInterceptFileChooserDialog",
)


class BoronPipeRejected(ValueError):
    """A pipe message, browser configuration, or lifecycle failed closed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class BoronNavigationState(str, Enum):
    STARTING = "starting"
    CONFIGURING = "configuring"
    NAVIGATING = "navigating"
    VERIFIED = "verified"
    HANDOFF = "handoff"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _reject(reason_code: str) -> NoReturn:
    raise BoronPipeRejected(reason_code)


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("json_duplicate_key")
        result[key] = value
    return result


def _constant(_value: str) -> NoReturn:
    _reject("json_constant")


def _bound_tree(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    if depth > BORON_MAX_JSON_DEPTH:
        _reject("json_depth")
    count[0] += 1
    if count[0] > BORON_MAX_JSON_ITEMS:
        _reject("json_items")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        _reject("json_float")
    if type(value) is str:
        if len(value.encode("utf-8")) > BORON_MAX_STRING_BYTES:
            _reject("json_string")
        return
    if type(value) is list:
        for item in value:
            _bound_tree(item, depth=depth + 1, count=count)
        return
    if type(value) is dict:
        for key, item in value.items():
            _bound_tree(key, depth=depth + 1, count=count)
            _bound_tree(item, depth=depth + 1, count=count)
        return
    _reject("json_type")


def decode_boron_pipe_message(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > BORON_MAX_PIPE_MESSAGE_BYTES:
        _reject("pipe_frame_size")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except UnicodeDecodeError:
        _reject("pipe_frame_utf8")
    except json.JSONDecodeError:
        _reject("pipe_frame_json")
    if type(value) is not dict:
        _reject("pipe_message_object")
    _bound_tree(value)
    return value


def encode_boron_pipe_message(message: Mapping[str, Any]) -> bytes:
    if type(message) is not dict:
        _reject("pipe_message_object")
    _bound_tree(message)
    try:
        payload = json.dumps(
            message,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        _reject("pipe_frame_json")
    if not payload or len(payload) > BORON_MAX_PIPE_MESSAGE_BYTES:
        _reject("pipe_frame_size")
    return payload + b"\x00"


class BoronPipeDecoder:
    """Incremental NUL-delimited DevTools decoder with a fixed memory bound."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if type(chunk) is not bytes:
            _reject("pipe_feed_type")
        if len(self._buffer) + len(chunk) > BORON_MAX_PIPE_BUFFER_BYTES:
            _reject("pipe_buffer_size")
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []
        while True:
            try:
                end = self._buffer.index(0)
            except ValueError:
                if len(self._buffer) > BORON_MAX_PIPE_MESSAGE_BYTES:
                    _reject("pipe_frame_size")
                break
            if end == 0 or end > BORON_MAX_PIPE_MESSAGE_BYTES:
                _reject("pipe_frame_size")
            payload = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            messages.append(decode_boron_pipe_message(payload))
        return messages

    def finish(self) -> None:
        if self._buffer:
            _reject("pipe_frame_truncated")


def _canonical_https_origin(raw_url: str, *, allow_fragment: bool = False) -> tuple[str, str]:
    if type(raw_url) is not str or not raw_url or len(raw_url.encode("utf-8")) > 4096:
        _reject("navigation_url")
    if any(character in raw_url for character in ("\\", "\r", "\n", "\x00")):
        _reject("navigation_url")
    try:
        split = urlsplit(raw_url)
        port = split.port
    except ValueError:
        _reject("navigation_url")
    if (
        split.scheme.casefold() != "https"
        or split.username is not None
        or split.password is not None
        or not split.hostname
        or (port or 443) != 443
        or (split.fragment and not allow_fragment)
    ):
        _reject("navigation_url")
    host = split.hostname.rstrip(".").casefold()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            _reject("navigation_url")
        if not _HOST_RE.fullmatch(host) or ".." in host:
            _reject("navigation_url")
    else:
        # The public route deliberately rejects every numeric hostname.  The
        # egress module resolves names to pinned addresses independently.
        _reject("navigation_numeric_host")
    path = split.path or "/"
    canonical = f"https://{host}{path}"
    if split.query:
        canonical += "?" + split.query
    return canonical, f"https://{host}"


@dataclass(frozen=True, slots=True)
class BoronNavigationPlan:
    canonical_url: str
    expected_browser_version: str
    proxy_host: str = "xenon-egress"
    proxy_port: int = 3128
    maximum_duration_ms: int = 60_000

    def __post_init__(self) -> None:
        canonical, _origin = _canonical_https_origin(self.canonical_url)
        if canonical != self.canonical_url:
            _reject("navigation_not_canonical")
        if type(self.expected_browser_version) is not str or not _VERSION_RE.fullmatch(
            self.expected_browser_version
        ):
            _reject("browser_version")
        if type(self.proxy_host) is not str or not _HOST_RE.fullmatch(self.proxy_host):
            _reject("proxy_host")
        if type(self.proxy_port) is not int or not 1024 <= self.proxy_port <= 65535:
            _reject("proxy_port")
        if (
            type(self.maximum_duration_ms) is not int
            or not 1_000 <= self.maximum_duration_ms <= BORON_MAX_NAVIGATION_MS
        ):
            _reject("navigation_duration")

    @property
    def origin(self) -> str:
        return _canonical_https_origin(self.canonical_url)[1]

    def chrome_argv(self) -> tuple[str, ...]:
        """Return the complete fixed Chromium command; no extra flags exist."""

        return (
            BORON_CHROME_PATH,
            "--headless=new",
            "--remote-debugging-pipe=JSON",
            f"--user-data-dir={BORON_PROFILE_PATH}",
            f"--proxy-server=http://{self.proxy_host}:{self.proxy_port}",
            "--proxy-bypass-list=<-loopback>",
            "--disable-quic",
            "--disable-background-networking",
            "--disable-background-mode",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-notifications",
            "--disable-print-preview",
            "--disable-pdf-extension",
            "--deny-permission-prompts",
            "--disk-cache-size=0",
            "--media-cache-size=0",
            "--metrics-recording-only",
            "--no-default-browser-check",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
            "about:blank",
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def install_ephemeral_xenon_ca(
    ca_pem: bytes,
    *,
    now_ms: int,
    profile_path: Path = BORON_PROFILE_PATH,
    certutil_path: str = BORON_CERTUTIL_PATH,
    runner: Runner = subprocess.run,
) -> str:
    """Import one short-lived CA into the ephemeral profile and read it back."""

    if type(ca_pem) is not bytes or not 1 <= len(ca_pem) <= 16_384:
        _reject("ca_size")
    if type(now_ms) is not int or not 1 <= now_ms <= (1 << 53) - 1:
        _reject("ca_clock")
    if not isinstance(profile_path, Path) or not profile_path.is_absolute():
        _reject("profile_path")
    if type(certutil_path) is not str or certutil_path != BORON_CERTUTIL_PATH:
        _reject("certutil_path")
    try:
        certificate = x509.load_pem_x509_certificate(ca_pem)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except (ValueError, x509.ExtensionNotFound):
        _reject("ca_certificate")
    if not constraints.ca or constraints.path_length != 0:
        _reject("ca_constraints")
    now_seconds = now_ms // 1000
    if not (
        int(certificate.not_valid_before_utc.timestamp()) <= now_seconds
        < int(certificate.not_valid_after_utc.timestamp())
    ):
        _reject("ca_validity")

    try:
        profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if profile_path.is_symlink() or not profile_path.is_dir():
            _reject("profile_path")
        os.chmod(profile_path, 0o700)
        ca_path = profile_path / BORON_CA_PATH.name
        descriptor = os.open(ca_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(ca_pem)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    except BoronPipeRejected:
        raise
    except OSError:
        _reject("ca_write")

    commands: Sequence[Sequence[str]] = (
        (certutil_path, "-N", "-d", f"sql:{profile_path}", "--empty-password"),
        (
            certutil_path,
            "-A",
            "-d",
            f"sql:{profile_path}",
            "-n",
            "algo-xenon-session",
            "-t",
            "C,,",
            "-i",
            str(ca_path),
        ),
        (
            certutil_path,
            "-L",
            "-d",
            f"sql:{profile_path}",
            "-n",
            "algo-xenon-session",
            "-a",
        ),
    )
    result: subprocess.CompletedProcess[str] | None = None
    try:
        for command in commands:
            result = runner(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            if result.returncode != 0:
                _reject("ca_import")
        if result is None:
            _reject("ca_import")
        installed = x509.load_pem_x509_certificate(result.stdout.encode("ascii"))
        expected_digest = certificate.fingerprint(hashes.SHA256()).hex()
        if installed.fingerprint(hashes.SHA256()).hex() != expected_digest:
            _reject("ca_readback")
        return "sha256:" + expected_digest
    except BoronPipeRejected:
        raise
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        _reject("ca_import")
    finally:
        try:
            ca_path.unlink(missing_ok=True)
        except OSError:
            pass


def _command(
    command_id: int,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    if type(command_id) is not int or not 1 <= command_id <= (1 << 31) - 1:
        _reject("command_id")
    if type(method) is not str or method not in _ALLOWED_METHODS:
        _reject("cdp_method")
    if params is None:
        params = {}
    if type(params) is not dict:
        _reject("cdp_params")
    row: dict[str, Any] = {"id": command_id, "method": method, "params": dict(params)}
    if session_id is not None:
        if type(session_id) is not str or not session_id or len(session_id) > 256:
            _reject("cdp_session")
        row["sessionId"] = session_id
    _bound_tree(row)
    return row


@dataclass(frozen=True, slots=True)
class BoronNavigationEvidence:
    state: BoronNavigationState
    browser_major: int
    command_count: int
    event_count: int
    child_frame_count: int
    origin_digest: str
    frame_digest: str
    loader_digest: str
    reason_code: str


class BoronNavigationMachine:
    """Closed CDP state machine for exactly one top-frame navigation."""

    def __init__(self, plan: BoronNavigationPlan) -> None:
        if type(plan) is not BoronNavigationPlan:
            _reject("navigation_plan")
        self.plan = plan
        self.state = BoronNavigationState.STARTING
        self._next_id = 1
        self._pending: dict[int, str] = {}
        self._browser_context_id: str | None = None
        self._target_id: str | None = None
        self._session_id: str | None = None
        self._frame_id: str | None = None
        self._loader_id: str | None = None
        self._configured: set[str] = set()
        self._navigate_acked = False
        self._target_committed = False
        self._matching_load_seen = False
        self._command_count = 0
        self._event_count = 0
        self._child_frames = 0
        self._browser_major = 0
        self._reason = "not_complete"

    @staticmethod
    def _opaque_digest(label: str, value: str) -> str:
        return "sha256:" + hashlib.sha256((label + "\x00" + value).encode("utf-8")).hexdigest()

    def _make(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        page: bool = False,
    ) -> dict[str, Any]:
        command_id = self._next_id
        self._next_id += 1
        row = _command(
            command_id,
            method,
            params,
            session_id=self._session_id if page else None,
        )
        self._pending[command_id] = method
        self._command_count += 1
        return row

    def start(self) -> tuple[dict[str, Any], ...]:
        if self._pending or self.state is not BoronNavigationState.STARTING:
            _reject("machine_started")
        self.state = BoronNavigationState.CONFIGURING
        return (self._make("Browser.getVersion"),)

    def _terminal(self, state: BoronNavigationState, reason: str) -> None:
        if self.state is BoronNavigationState.VERIFIED:
            _reject("terminal_transition")
        self.state = state
        self._reason = reason

    def _maybe_verify(self) -> None:
        if self._navigate_acked and self._target_committed and self._matching_load_seen:
            self.state = BoronNavigationState.VERIFIED
            self._reason = "verified"

    def _result(self, message: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        if frozenset(message) - {"id", "result", "error", "sessionId"}:
            _reject("cdp_response_schema")
        command_id = message.get("id")
        if type(command_id) is not int or command_id not in self._pending:
            _reject("cdp_response_id")
        method = self._pending.pop(command_id)
        if "error" in message:
            self._terminal(BoronNavigationState.FAILED, "cdp_command_failed")
            return ()
        result = message.get("result")
        if type(result) is not dict:
            _reject("cdp_result")

        if method == "Browser.getVersion":
            product = result.get("product")
            protocol = result.get("protocolVersion")
            if type(product) is not str or "/" not in product or type(protocol) is not str:
                _reject("browser_identity")
            product_name, version = product.rsplit("/", 1)
            if product_name not in {"Chrome", "Chromium", "HeadlessChrome"}:
                _reject("browser_identity")
            if version != self.plan.expected_browser_version or protocol != "1.3":
                _reject("browser_version_skew")
            self._browser_major = int(version.split(".", 1)[0])
            return (
                self._make(
                    "Target.createBrowserContext",
                    {"disposeOnDetach": True},
                ),
            )

        if method == "Target.createBrowserContext":
            context = result.get("browserContextId")
            if type(context) is not str or not context or len(context) > 256:
                _reject("browser_context")
            self._browser_context_id = context
            return (
                self._make(
                    "Browser.setDownloadBehavior",
                    {"behavior": "deny", "browserContextId": context},
                ),
                self._make(
                    "Target.createTarget",
                    {"url": "about:blank", "browserContextId": context},
                ),
            )

        if method == "Target.createTarget":
            target = result.get("targetId")
            if type(target) is not str or not target or len(target) > 256:
                _reject("target_id")
            self._target_id = target
            return (
                self._make(
                    "Target.attachToTarget",
                    {"targetId": target, "flatten": True},
                ),
            )

        if method == "Target.attachToTarget":
            session = result.get("sessionId")
            if type(session) is not str or not session or len(session) > 256:
                _reject("cdp_session")
            self._session_id = session
            return (
                self._make(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                    page=True,
                ),
                self._make("Page.enable", page=True),
                self._make("Network.enable", {"maxTotalBufferSize": 0}, page=True),
                self._make(
                    "Network.setBlockedURLs",
                    {"urls": ["ws://*", "wss://*"]},
                    page=True,
                ),
                self._make("Page.setLifecycleEventsEnabled", {"enabled": True}, page=True),
                self._make(
                    "Page.setInterceptFileChooserDialog",
                    {"enabled": True, "cancel": True},
                    page=True,
                ),
            )

        if method in _BOOTSTRAP_PAGE_METHODS or method in {
            "Browser.setDownloadBehavior",
            "Target.setAutoAttach",
        }:
            self._configured.add(method)
            required = set(_BOOTSTRAP_PAGE_METHODS) | {
                "Browser.setDownloadBehavior",
                "Target.setAutoAttach",
            }
            if required <= self._configured and self.state is BoronNavigationState.CONFIGURING:
                self.state = BoronNavigationState.NAVIGATING
                return (
                    self._make(
                        "Page.navigate",
                        {"url": self.plan.canonical_url, "transitionType": "typed"},
                        page=True,
                    ),
                )
            return ()

        if method == "Page.navigate":
            if result.get("errorText") not in (None, ""):
                self._terminal(BoronNavigationState.FAILED, "navigation_failed")
                return ()
            if result.get("isDownload") is True:
                self._terminal(BoronNavigationState.HANDOFF, "download_denied")
                return ()
            frame = result.get("frameId")
            loader = result.get("loaderId")
            if type(frame) is not str or not frame or type(loader) is not str or not loader:
                _reject("navigation_identity")
            self._frame_id = frame
            self._loader_id = loader
            self._navigate_acked = True
            self._maybe_verify()
            return ()

        if method in {"Page.stopLoading", "Target.disposeBrowserContext"}:
            return ()
        _reject("cdp_result_method")

    def _event(self, message: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        if frozenset(message) - {"method", "params", "sessionId"}:
            _reject("cdp_event_schema")
        method = message.get("method")
        params = message.get("params", {})
        if type(method) is not str or type(params) is not dict:
            _reject("cdp_event")
        self._event_count += 1

        terminal_events = {
            "Inspector.targetCrashed": (BoronNavigationState.UNKNOWN, "target_crashed"),
            "Target.targetCrashed": (BoronNavigationState.UNKNOWN, "target_crashed"),
            "Target.detachedFromTarget": (BoronNavigationState.UNKNOWN, "target_detached"),
            "Page.javascriptDialogOpening": (BoronNavigationState.HANDOFF, "dialog_handoff"),
            "Page.windowOpen": (BoronNavigationState.HANDOFF, "popup_handoff"),
            "Page.fileChooserOpened": (BoronNavigationState.HANDOFF, "upload_handoff"),
            "Browser.downloadWillBegin": (BoronNavigationState.HANDOFF, "download_denied"),
            "Page.downloadWillBegin": (BoronNavigationState.HANDOFF, "download_denied"),
            "Network.webSocketCreated": (BoronNavigationState.FAILED, "websocket_denied"),
        }
        if method in terminal_events:
            state, reason = terminal_events[method]
            self._terminal(state, reason)
            if self._session_id is not None:
                return (self._make("Page.stopLoading", page=True),)
            return ()

        if method == "Target.attachedToTarget":
            info = params.get("targetInfo")
            if type(info) is not dict or info.get("targetId") != self._target_id:
                self._terminal(BoronNavigationState.HANDOFF, "unexpected_target")
            return ()

        if method == "Page.frameAttached":
            frame = params.get("frameId")
            parent = params.get("parentFrameId")
            if type(frame) is not str or type(parent) is not str:
                _reject("frame_event")
            self._child_frames += 1
            return ()

        if method == "Page.frameDetached":
            if params.get("frameId") == self._frame_id:
                self._terminal(BoronNavigationState.UNKNOWN, "top_frame_detached")
            return ()

        if method == "Page.frameNavigated":
            frame = params.get("frame")
            if type(frame) is not dict:
                _reject("frame_event")
            if "parentId" in frame:
                return ()
            frame_id = frame.get("id")
            loader_id = frame.get("loaderId")
            url = frame.get("url")
            if type(frame_id) is not str or type(loader_id) is not str or type(url) is not str:
                _reject("frame_event")
            if url == "about:blank" and not self._navigate_acked:
                return ()
            _canonical, origin = _canonical_https_origin(url, allow_fragment=True)
            if origin != self.plan.origin:
                self._terminal(BoronNavigationState.FAILED, "origin_drift")
                return ()
            if self._frame_id is not None and frame_id != self._frame_id:
                self._terminal(BoronNavigationState.FAILED, "frame_drift")
                return ()
            if self._loader_id is not None and loader_id != self._loader_id:
                self._terminal(BoronNavigationState.FAILED, "loader_drift")
                return ()
            self._frame_id = frame_id
            self._loader_id = loader_id
            self._target_committed = True
            self._maybe_verify()
            return ()

        if method == "Page.navigatedWithinDocument":
            if params.get("frameId") != self._frame_id or type(params.get("url")) is not str:
                _reject("same_document_event")
            _canonical, origin = _canonical_https_origin(
                params["url"], allow_fragment=True
            )
            if origin != self.plan.origin:
                self._terminal(BoronNavigationState.FAILED, "origin_drift")
            return ()

        if method == "Page.lifecycleEvent":
            if params.get("name") != "load":
                return ()
            if not self._navigate_acked and not self._target_committed:
                return ()
            if params.get("frameId") != self._frame_id or params.get("loaderId") != self._loader_id:
                self._terminal(BoronNavigationState.FAILED, "lifecycle_drift")
                return ()
            if self._frame_id is None or self._loader_id is None:
                _reject("lifecycle_identity")
            self._matching_load_seen = True
            self._maybe_verify()
            return ()

        # Unknown events are ignored structurally. They cannot expand the method
        # vocabulary, but their payload is still bounded by the pipe decoder.
        return ()

    def handle(self, message: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        if type(message) is not dict:
            _reject("pipe_message_object")
        _bound_tree(message)
        if self.state in {
            BoronNavigationState.VERIFIED,
            BoronNavigationState.FAILED,
            BoronNavigationState.HANDOFF,
            BoronNavigationState.UNKNOWN,
        }:
            _reject("machine_terminal")
        if "id" in message:
            return self._result(message)
        if "method" in message:
            return self._event(message)
        _reject("cdp_message_kind")

    def evidence(self) -> BoronNavigationEvidence:
        if self.state not in {
            BoronNavigationState.VERIFIED,
            BoronNavigationState.FAILED,
            BoronNavigationState.HANDOFF,
            BoronNavigationState.UNKNOWN,
        }:
            _reject("evidence_not_terminal")
        frame = self._frame_id or "none"
        loader = self._loader_id or "none"
        return BoronNavigationEvidence(
            state=self.state,
            browser_major=self._browser_major,
            command_count=self._command_count,
            event_count=self._event_count,
            child_frame_count=self._child_frames,
            origin_digest=self._opaque_digest("origin", self.plan.origin),
            frame_digest=self._opaque_digest("frame", frame),
            loader_digest=self._opaque_digest("loader", loader),
            reason_code=self._reason,
        )


@dataclass(slots=True)
class BoronPidProcess:
    pid: int
    _returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        try:
            found, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            _reject("browser_process_identity")
        if found == 0:
            return None
        if found != self.pid:
            _reject("browser_process_identity")
        self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def wait(self, *, timeout: float) -> int:
        if type(timeout) is not float or not 0.1 <= timeout <= 30.0:
            _reject("browser_wait_timeout")
        deadline = time.monotonic() + timeout
        while True:
            result = self.poll()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(BORON_CHROME_PATH, timeout)
            time.sleep(0.01)

    def terminate(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            _reject("browser_terminate")

    def kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            _reject("browser_kill")


@dataclass(slots=True)
class BoronChromeProcess:
    process: BoronPidProcess
    write_fd: int
    read_fd: int

    def send(self, message: Mapping[str, Any]) -> None:
        payload = encode_boron_pipe_message(message)
        view = memoryview(payload)
        while view:
            try:
                written = os.write(self.write_fd, view)
            except OSError:
                _reject("pipe_write")
            if written <= 0:
                _reject("pipe_write")
            view = view[written:]

    def receive(self, decoder: BoronPipeDecoder, *, deadline: float) -> list[dict[str, Any]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reject("navigation_timeout")
        ready, _, _ = select.select([self.read_fd], [], [], remaining)
        if not ready:
            _reject("navigation_timeout")
        try:
            chunk = os.read(self.read_fd, 65_536)
        except OSError:
            _reject("pipe_read")
        if not chunk:
            _reject("browser_disconnected")
        return decoder.feed(chunk)

    def close(self) -> None:
        for descriptor in (self.write_fd, self.read_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3.0)


def launch_boron_chrome(
    plan: BoronNavigationPlan,
) -> BoronChromeProcess:
    """Launch fixed Chrome with DevTools endpoints inherited as fds 3 and 4."""

    if type(plan) is not BoronNavigationPlan:
        _reject("navigation_plan")
    controller_to_chrome_read, controller_to_chrome_write = os.pipe()
    chrome_to_controller_read, chrome_to_controller_write = os.pipe()
    null_fd = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
    child_read_fd = fcntl.fcntl(
        controller_to_chrome_read,
        fcntl.F_DUPFD_CLOEXEC,
        10,
    )
    child_write_fd = fcntl.fcntl(
        chrome_to_controller_write,
        fcntl.F_DUPFD_CLOEXEC,
        10,
    )
    sources = {
        controller_to_chrome_read,
        controller_to_chrome_write,
        chrome_to_controller_read,
        chrome_to_controller_write,
        null_fd,
        child_read_fd,
        child_write_fd,
    }
    file_actions: list[tuple[int, ...]] = [
        (os.POSIX_SPAWN_DUP2, null_fd, 0),
        (os.POSIX_SPAWN_DUP2, null_fd, 1),
        (os.POSIX_SPAWN_DUP2, null_fd, 2),
        (os.POSIX_SPAWN_DUP2, child_read_fd, 3),
        (os.POSIX_SPAWN_DUP2, child_write_fd, 4),
    ]
    for descriptor in sorted(sources - {3, 4}):
        file_actions.append((os.POSIX_SPAWN_CLOSE, descriptor))
    environment = {
        "HOME": "/home/algo",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }
    try:
        pid = os.posix_spawn(
            BORON_CHROME_PATH,
            list(plan.chrome_argv()),
            environment,
            file_actions=file_actions,
            setsid=True,
        )
    except (NotImplementedError, OSError, TypeError, ValueError):
        for descriptor in (
            controller_to_chrome_read,
            controller_to_chrome_write,
            chrome_to_controller_read,
            chrome_to_controller_write,
            null_fd,
            child_read_fd,
            child_write_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        _reject("browser_launch")
    os.close(controller_to_chrome_read)
    os.close(chrome_to_controller_write)
    os.close(null_fd)
    os.close(child_read_fd)
    os.close(child_write_fd)
    return BoronChromeProcess(
        BoronPidProcess(pid),
        controller_to_chrome_write,
        chrome_to_controller_read,
    )


def run_boron_navigation(plan: BoronNavigationPlan) -> BoronNavigationEvidence:
    """Run the finite state machine until one terminal navigation observation."""

    machine = BoronNavigationMachine(plan)
    decoder = BoronPipeDecoder()
    browser = launch_boron_chrome(plan)
    deadline = time.monotonic() + plan.maximum_duration_ms / 1000
    try:
        for command in machine.start():
            browser.send(command)
        while machine.state not in {
            BoronNavigationState.VERIFIED,
            BoronNavigationState.FAILED,
            BoronNavigationState.HANDOFF,
            BoronNavigationState.UNKNOWN,
        }:
            for message in browser.receive(decoder, deadline=deadline):
                for command in machine.handle(message):
                    browser.send(command)
        return machine.evidence()
    finally:
        browser.close()


__all__ = [
    "BORON_CA_PATH",
    "BORON_CHROME_PATH",
    "BORON_MAX_PIPE_BUFFER_BYTES",
    "BORON_MAX_PIPE_MESSAGE_BYTES",
    "BORON_PIPE_PROTOCOL_VERSION",
    "BoronChromeProcess",
    "BoronPidProcess",
    "BoronNavigationEvidence",
    "BoronNavigationMachine",
    "BoronNavigationPlan",
    "BoronNavigationState",
    "BoronPipeDecoder",
    "BoronPipeRejected",
    "decode_boron_pipe_message",
    "encode_boron_pipe_message",
    "install_ephemeral_xenon_ca",
    "launch_boron_chrome",
    "run_boron_navigation",
]
