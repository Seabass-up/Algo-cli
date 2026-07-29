"""Truthful Echo Veil status and source-qualified package updates.

This module owns Echo Veil package maintenance so an LLM never needs to use a
generic shell action, edit ``site-packages``, or infer whether an update worked.
Only the reviewed Algo-qualified source revision may be installed. Candidates
are built and exercised in staging before the active interpreter is changed,
then verified again in a fresh isolated process. A failed mutation triggers a
best-effort rollback to the previously qualified version.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ECHO_VEIL_DISTRIBUTION = "echo-veil"
ECHO_VEIL_REPOSITORY = "https://github.com/Seabass-up/echo-veil"
ECHO_VEIL_REPOSITORY_GIT = f"{ECHO_VEIL_REPOSITORY}.git"
QUALIFIED_ECHO_VEIL_VERSION = "0.7.0"
QUALIFIED_ECHO_VEIL_COMMIT = "e94be9e649048273ab74eb1150e65ac9481596d9"
QUALIFIED_ECHO_VEIL_REQUIREMENT = (
    f"{ECHO_VEIL_DISTRIBUTION} @ "
    f"git+{ECHO_VEIL_REPOSITORY_GIT}@{QUALIFIED_ECHO_VEIL_COMMIT}"
)
PREVIOUS_QUALIFIED_COMMITS: Mapping[str, str] = {
    "0.6.0": "8bd84141580508eec13bd0459634cf3153c32eae",
    QUALIFIED_ECHO_VEIL_VERSION: QUALIFIED_ECHO_VEIL_COMMIT,
}
UPDATE_TIMEOUT_SECONDS = 600
VERIFY_TIMEOUT_SECONDS = 90
UPSTREAM_TIMEOUT_SECONDS = 3.0
_VERSION_RE = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:[.+-][0-9A-Za-z.-]+)?$"
)
_HEX_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True)
class EchoStatus:
    installed: bool
    installed_version: str | None
    source_version: str | None
    installation_kind: str
    source_url: str | None
    source_commit: str | None
    api_contract_ready: bool
    adapter_supported: bool
    enabled: bool
    protection_policy: str
    healthy: bool
    local_protection_ready: bool
    qualified: bool
    upstream_version: str | None
    upstream_error: str | None

    @property
    def upstream_current(self) -> bool | None:
        if self.upstream_version is None or self.installed_version is None:
            return None
        return _version_tuple(self.installed_version) == _version_tuple(
            self.upstream_version
        )

    @property
    def exit_code(self) -> int:
        if not self.qualified:
            return 1
        if self.enabled and self.protection_policy == "required" and not self.healthy:
            return 2
        return 0


@dataclass(frozen=True)
class EchoUpdateResult:
    returncode: int
    before_version: str | None
    after_version: str | None
    changed: bool
    message: str
    details: str = ""
    rollback_attempted: bool = False
    rollback_succeeded: bool = False


@dataclass(frozen=True)
class _Installer:
    kind: str
    command: str


def _select_installer(
    *,
    requested: str | None,
    executable: str,
    which: Callable[[str], str | None],
) -> _Installer:
    choice = str(requested or "").strip().casefold()
    if choice and choice not in {"pip", "uv"}:
        raise ValueError("Echo installer must be pip or uv")
    if choice == "pip":
        return _Installer("pip", executable)
    if choice == "uv":
        uv = which("uv")
        if uv is None:
            raise RuntimeError("uv was requested but is not available on PATH")
        return _Installer("uv", uv)
    if importlib.util.find_spec("pip") is not None:
        return _Installer("pip", executable)
    uv = which("uv")
    if uv is not None:
        return _Installer("uv", uv)
    raise RuntimeError(
        "Neither the running interpreter's pip module nor uv is available"
    )


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    clean = str(value or "").strip().lstrip("v")
    match = _VERSION_RE.fullmatch(clean)
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]
    except ValueError:
        return None


def _bounded_details(*parts: str, limit: int = 4_000) -> str:
    combined = "\n".join(part.strip() for part in parts if part and part.strip())
    if len(combined) <= limit:
        return combined
    return "…" + combined[-(limit - 1) :]


def _direct_url_identity(distribution: Any) -> tuple[str, str | None, str | None]:
    try:
        raw = distribution.read_text("direct_url.json")
    except (OSError, UnicodeError, ValueError):
        return "direct-url-unreadable", None, None
    if raw is None:
        return "registry-or-wheel", None, None
    if not isinstance(raw, str) or not 1 <= len(raw.encode("utf-8")) <= 16_384:
        return "direct-url-unreadable", None, None
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return "direct-url-unreadable", None, None
    if not isinstance(document, dict):
        return "direct-url-unreadable", None, None
    source_url = str(document.get("url") or "").strip() or None
    directory = document.get("dir_info")
    if isinstance(directory, dict) and directory.get("editable") is True:
        return "editable", source_url, None
    vcs = document.get("vcs_info")
    if isinstance(vcs, dict):
        commit = str(vcs.get("commit_id") or "").strip().lower()
        if _HEX_COMMIT_RE.fullmatch(commit):
            return "vcs-pinned", source_url, commit
        return "vcs-unpinned", source_url, None
    archive = document.get("archive_info")
    if isinstance(archive, dict):
        hash_value = str(archive.get("hash") or "")
        hashes = archive.get("hashes")
        sha256_value = (
            str(hashes.get("sha256") or "") if isinstance(hashes, dict) else ""
        )
        pinned = bool(
            re.fullmatch(r"sha256=[0-9a-fA-F]{64}", hash_value)
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256_value)
        )
        return (
            "archive-pinned" if pinned else "archive-unpinned",
            source_url,
            None,
        )
    return "direct-url-unpinned", source_url, None


def _canonical_repository(url: str | None) -> bool:
    clean = str(url or "").strip().casefold()
    for prefix in ("git+",):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]
    clean = clean.removesuffix("/").removesuffix(".git")
    return clean == ECHO_VEIL_REPOSITORY.casefold()


def fetch_upstream_latest(
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: float = UPSTREAM_TIMEOUT_SECONDS,
) -> tuple[str | None, str | None]:
    """Read the latest release/tag from the canonical repository only."""

    endpoints = (
        "https://api.github.com/repos/Seabass-up/echo-veil/releases/latest",
        "https://api.github.com/repos/Seabass-up/echo-veil/tags?per_page=1",
    )
    failures: list[str] = []
    versions: list[str] = []
    for endpoint in endpoints:
        request = Request(
            endpoint,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "algo-cli-echo-status",
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                payload = response.read(1_048_577)
            if len(payload) > 1_048_576:
                failures.append("response_too_large")
                continue
            decoded = json.loads(payload.decode("utf-8"))
            if isinstance(decoded, dict):
                raw_version = decoded.get("tag_name")
            elif isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
                raw_version = decoded[0].get("name")
            else:
                raw_version = None
            version = str(raw_version or "").strip().lstrip("v")
            if _version_tuple(version) is not None:
                versions.append(version)
                continue
            failures.append("invalid_version")
        except HTTPError as exc:
            failures.append(f"http_{exc.code}")
        except (URLError, TimeoutError, OSError):
            failures.append("unavailable")
        except (json.JSONDecodeError, UnicodeError):
            failures.append("invalid_response")
    if versions:
        latest = max(
            versions,
            key=lambda value: _version_tuple(value) or (0, 0, 0),
        )
        return latest, None
    reason = ",".join(dict.fromkeys(failures)) or "unavailable"
    return None, reason


def collect_echo_status(
    config: object,
    *,
    include_upstream: bool = True,
    distribution_getter: Callable[[str], Any] = metadata.distribution,
    module_importer: Callable[[str], Any] = importlib.import_module,
    upstream_fetcher: Callable[[], tuple[str | None, str | None]] = fetch_upstream_latest,
    readiness_getter: Callable[[object], dict[str, Any]] | None = None,
) -> EchoStatus:
    """Collect independently labeled package, adapter, doctor, and upstream facts."""

    try:
        distribution = distribution_getter(ECHO_VEIL_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        distribution = None
    except Exception:
        distribution = None
    installed_version = (
        str(getattr(distribution, "version", "") or "").strip() or None
        if distribution is not None
        else None
    )
    if distribution is None:
        installation_kind, source_url, source_commit = "missing", None, None
    else:
        installation_kind, source_url, source_commit = _direct_url_identity(
            distribution
        )

    source_version: str | None = None
    api_contract_ready = False
    try:
        package = module_importer("echo_veil")
        source_version = str(getattr(package, "__version__", "") or "").strip() or None
        agent_memory = module_importer("echo_veil.agent_memory")
        api_contract_ready = all(
            hasattr(agent_memory, name)
            for name in (
                "AgentMemory",
                "AlwaysAvailableMemory",
                "EmbeddingUnavailable",
                "HashingTextEmbedder",
                "OllamaTextEmbedder",
            )
        )
    except (ImportError, AttributeError):
        pass

    if readiness_getter is None:
        try:
            from .ada_memory_echo_veil import get_echo_veil_readiness

            readiness_getter = get_echo_veil_readiness
        except ImportError:
            readiness_getter = None
    readiness: dict[str, Any] = {}
    if readiness_getter is not None:
        try:
            readiness = readiness_getter(config)
        except Exception:
            readiness = {}

    upstream_version: str | None = None
    upstream_error: str | None = None
    if include_upstream:
        upstream_version, upstream_error = upstream_fetcher()

    adapter_supported = bool(readiness.get("version_supported", False))
    qualified = bool(
        installed_version == QUALIFIED_ECHO_VEIL_VERSION
        and source_version == QUALIFIED_ECHO_VEIL_VERSION
        and installation_kind == "vcs-pinned"
        and source_commit == QUALIFIED_ECHO_VEIL_COMMIT
        and _canonical_repository(source_url)
        and api_contract_ready
        and adapter_supported
    )
    return EchoStatus(
        installed=distribution is not None,
        installed_version=installed_version,
        source_version=source_version,
        installation_kind=installation_kind,
        source_url=source_url,
        source_commit=source_commit,
        api_contract_ready=api_contract_ready,
        adapter_supported=adapter_supported,
        enabled=bool(readiness.get("enabled", False)),
        protection_policy=str(readiness.get("protection_policy") or "unknown"),
        healthy=bool(readiness.get("healthy", False)),
        local_protection_ready=bool(
            readiness.get("local_protection_ready", False)
        ),
        qualified=qualified,
        upstream_version=upstream_version,
        upstream_error=upstream_error,
    )


def render_echo_status(status: EchoStatus) -> str:
    installed = status.installed_version or "not installed"
    commit = (
        status.source_commit[:12]
        if status.source_commit is not None
        else "unverified"
    )
    upstream = (
        f"{status.upstream_version} (canonical repository verified)"
        if status.upstream_version is not None
        else f"unknown ({status.upstream_error or 'unavailable'})"
    )
    if status.upstream_current is True:
        upstream_state = "yes"
    elif status.upstream_current is False:
        upstream_state = "no"
    else:
        upstream_state = "unknown"
    doctor_state = (
        "healthy"
        if status.healthy
        else ("disabled" if not status.enabled else "not healthy")
    )
    return "\n".join(
        (
            "Echo Veil status",
            f"Installed: {installed} ({status.installation_kind}, {commit})",
            (
                "Algo qualified: "
                f"{'yes' if status.qualified else 'no'} "
                f"(requires {QUALIFIED_ECHO_VEIL_VERSION} @ "
                f"{QUALIFIED_ECHO_VEIL_COMMIT[:12]})"
            ),
            f"Adapter/API: {'supported' if status.adapter_supported and status.api_contract_ready else 'unsupported'}",
            (
                f"Memory doctor: {doctor_state}; protection="
                f"{status.protection_policy}; shield-ready="
                f"{'yes' if status.local_protection_ready else 'no'}"
            ),
            f"Upstream latest: {upstream}",
            f"Installed equals verified upstream latest: {upstream_state}",
        )
    )


_LIFECYCLE_VERIFY_SCRIPT = r"""
import importlib.metadata
import inspect
import json
from pathlib import Path
import sys
import tempfile

stage = sys.argv[1]
expected_version = sys.argv[2]
expected_commit = sys.argv[3]
require_provenance = sys.argv[4] == "1"
if stage:
    sys.path.insert(0, stage)

import echo_veil
from echo_veil.agent_memory import (
    AgentMemory,
    AlwaysAvailableMemory,
    EmbeddingUnavailable,
    HashingTextEmbedder,
    OllamaTextEmbedder,
)

assert echo_veil.__version__ == expected_version
assert all((AgentMemory, AlwaysAvailableMemory, EmbeddingUnavailable, HashingTextEmbedder, OllamaTextEmbedder))

if require_provenance:
    distribution = importlib.metadata.distribution("echo-veil")
    direct = json.loads(distribution.read_text("direct_url.json"))
    vcs = direct.get("vcs_info") or {}
    assert vcs.get("commit_id") == expected_commit
    source_url = str(direct.get("url") or "").lower()
    assert source_url.removeprefix("git+").removesuffix("/").removesuffix(".git") == "https://github.com/seabass-up/echo-veil"

secret = "algo qualified lifecycle seed 731"
with tempfile.TemporaryDirectory(prefix="algo-echo-verify-") as raw_root:
    # macOS exposes its temporary root through /var -> /private/var. Echo
    # deliberately rejects symlinked state paths, so canonicalize the already
    # created private directory before opening the protected profile.
    root = Path(raw_root).resolve(strict=True)
    embedder = HashingTextEmbedder()
    with AgentMemory(
        root,
        profile="algo-update-preflight",
        scope="algo:update-preflight",
        embed=embedder,
    ) as memory:
        remember_kwargs = {}
        if "provenance" in inspect.signature(memory.remember).parameters:
            remember_kwargs["provenance"] = ["operator:algo-update-preflight"]
        memory.remember("qualification", secret, **remember_kwargs)
        recalled = memory.recall(secret, top_k=2)
        assert recalled["results"][0]["payload"] == secret
        doctor = memory.doctor()
        assert doctor["readiness"]["healthy"] is True
    for path in root.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes()
    with AgentMemory(
        root,
        profile="algo-update-preflight",
        scope="algo:update-preflight",
        embed=HashingTextEmbedder(),
    ) as restored:
        recalled = restored.recall(secret, top_k=2)
        assert recalled["results"][0]["payload"] == secret
    try:
        AgentMemory(
            root,
            profile="algo-update-preflight",
            scope="algo:wrong-scope",
            embed=HashingTextEmbedder(),
        )
    except Exception:
        pass
    else:
        raise AssertionError("wrong scope unexpectedly opened protected state")

print(json.dumps({"version": echo_veil.__version__, "lifecycle": True, "provenance": require_provenance}))
"""


def _run(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: list[str],
    *,
    timeout: int,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=str(cwd),
    )


def _install_requirement(
    requirement: str,
    *,
    installer: _Installer,
    executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    if installer.kind == "uv":
        command = [
            installer.command,
            "pip",
            "install",
            "--python",
            executable,
            "--no-deps",
            "--upgrade",
            "--reinstall-package",
            ECHO_VEIL_DISTRIBUTION,
            requirement,
        ]
    else:
        command = [
            executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--upgrade",
            "--force-reinstall",
            requirement,
        ]
    return _run(
        runner,
        command,
        timeout=UPDATE_TIMEOUT_SECONDS,
        cwd=cwd,
    )


def _verify_installed(
    *,
    executable: str,
    version: str,
    commit: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return _run(
        runner,
        [
            executable,
            "-I",
            "-c",
            _LIFECYCLE_VERIFY_SCRIPT,
            "",
            version,
            commit,
            "1",
        ],
        timeout=VERIFY_TIMEOUT_SECONDS,
        cwd=cwd,
    )


def _rollback(
    *,
    before_version: str | None,
    installer: _Installer,
    executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    cwd: Path,
) -> tuple[bool, str]:
    if before_version is None:
        if installer.kind == "uv":
            command = [
                installer.command,
                "pip",
                "uninstall",
                "--python",
                executable,
                ECHO_VEIL_DISTRIBUTION,
            ]
        else:
            command = [
                executable,
                "-m",
                "pip",
                "uninstall",
                "--yes",
                ECHO_VEIL_DISTRIBUTION,
            ]
        completed = _run(
            runner,
            command,
            timeout=UPDATE_TIMEOUT_SECONDS,
            cwd=cwd,
        )
        return completed.returncode == 0, _bounded_details(
            completed.stdout, completed.stderr
        )
    previous_commit = PREVIOUS_QUALIFIED_COMMITS.get(before_version)
    if previous_commit is None:
        return False, "No reviewed rollback revision exists for the prior version."
    requirement = (
        f"{ECHO_VEIL_DISTRIBUTION} @ "
        f"git+{ECHO_VEIL_REPOSITORY_GIT}@{previous_commit}"
    )
    installed = _install_requirement(
        requirement,
        installer=installer,
        executable=executable,
        runner=runner,
        cwd=cwd,
    )
    if installed.returncode != 0:
        return False, _bounded_details(installed.stdout, installed.stderr)
    verified = _verify_installed(
        executable=executable,
        version=before_version,
        commit=previous_commit,
        runner=runner,
        cwd=cwd,
    )
    return verified.returncode == 0, _bounded_details(
        verified.stdout, verified.stderr
    )


def update_echo_veil(
    config: object,
    *,
    before: EchoStatus | None = None,
    executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    installer: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> EchoUpdateResult:
    """Install only Algo's qualified Echo revision after staged verification."""

    current = before or collect_echo_status(config, include_upstream=False)
    before_version = current.installed_version
    if current.qualified:
        return EchoUpdateResult(
            returncode=0,
            before_version=before_version,
            after_version=before_version,
            changed=False,
            message=(
                "Algo-qualified Echo Veil "
                f"{QUALIFIED_ECHO_VEIL_VERSION} is already installed. "
                "No package files were changed."
            ),
        )
    if (
        before_version is not None
        and before_version not in PREVIOUS_QUALIFIED_COMMITS
    ):
        return EchoUpdateResult(
            returncode=65,
            before_version=before_version,
            after_version=before_version,
            changed=False,
            message=(
                f"Echo Veil {before_version} is not a reviewed rollback source; "
                "the automatic update refused to mutate it."
            ),
        )

    python = executable or sys.executable
    try:
        selected_installer = _select_installer(
            requested=installer,
            executable=python,
            which=which,
        )
    except (RuntimeError, ValueError) as exc:
        return EchoUpdateResult(
            returncode=69,
            before_version=before_version,
            after_version=before_version,
            changed=False,
            message=f"Echo Veil update cannot start: {exc}.",
        )
    try:
        with tempfile.TemporaryDirectory(prefix="algo-echo-update-") as raw_root:
            root = Path(raw_root).resolve(strict=True)
            wheel_dir = root / "wheel"
            stage_dir = root / "stage"
            stage_dir.mkdir(mode=0o700)
            try:
                if selected_installer.kind == "uv":
                    staged = _run(
                        runner,
                        [
                            selected_installer.command,
                            "pip",
                            "install",
                            "--python",
                            python,
                            "--no-deps",
                            "--target",
                            str(stage_dir),
                            "--reinstall-package",
                            ECHO_VEIL_DISTRIBUTION,
                            QUALIFIED_ECHO_VEIL_REQUIREMENT,
                        ],
                        timeout=UPDATE_TIMEOUT_SECONDS,
                        cwd=root,
                    )
                else:
                    wheel_dir.mkdir(mode=0o700)
                    built = _run(
                        runner,
                        [
                            python,
                            "-m",
                            "pip",
                            "wheel",
                            "--disable-pip-version-check",
                            "--no-deps",
                            "--wheel-dir",
                            str(wheel_dir),
                            QUALIFIED_ECHO_VEIL_REQUIREMENT,
                        ],
                        timeout=UPDATE_TIMEOUT_SECONDS,
                        cwd=root,
                    )
                    if built.returncode != 0:
                        return EchoUpdateResult(
                            returncode=built.returncode,
                            before_version=before_version,
                            after_version=before_version,
                            changed=False,
                            message=(
                                "Echo Veil candidate build failed before "
                                "runtime mutation."
                            ),
                            details=_bounded_details(
                                built.stdout,
                                built.stderr,
                            ),
                        )
                    wheels = sorted(wheel_dir.glob("echo_veil-*.whl"))
                    if len(wheels) != 1:
                        return EchoUpdateResult(
                            returncode=1,
                            before_version=before_version,
                            after_version=before_version,
                            changed=False,
                            message=(
                                "Echo Veil candidate build produced an "
                                "unexpected wheel set."
                            ),
                        )
                    staged = _run(
                        runner,
                        [
                            python,
                            "-m",
                            "pip",
                            "install",
                            "--disable-pip-version-check",
                            "--no-deps",
                            "--target",
                            str(stage_dir),
                            str(wheels[0]),
                        ],
                        timeout=UPDATE_TIMEOUT_SECONDS,
                        cwd=root,
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return EchoUpdateResult(
                    returncode=124 if isinstance(exc, subprocess.TimeoutExpired) else 1,
                    before_version=before_version,
                    after_version=before_version,
                    changed=False,
                    message="Echo Veil staging could not complete.",
                    details=str(exc),
                )
            if staged.returncode != 0:
                return EchoUpdateResult(
                    returncode=staged.returncode,
                    before_version=before_version,
                    after_version=before_version,
                    changed=False,
                    message="Echo Veil staging install failed before runtime mutation.",
                    details=_bounded_details(staged.stdout, staged.stderr),
                )
            preflight = _run(
                runner,
                [
                    python,
                    "-I",
                    "-c",
                    _LIFECYCLE_VERIFY_SCRIPT,
                    str(stage_dir),
                    QUALIFIED_ECHO_VEIL_VERSION,
                    QUALIFIED_ECHO_VEIL_COMMIT,
                    "0",
                ],
                timeout=VERIFY_TIMEOUT_SECONDS,
                cwd=root,
            )
            if preflight.returncode != 0:
                return EchoUpdateResult(
                    returncode=preflight.returncode,
                    before_version=before_version,
                    after_version=before_version,
                    changed=False,
                    message=(
                        "Echo Veil candidate failed API, encryption, lifecycle, "
                        "scope, doctor, or restart preflight. The runtime was not changed."
                    ),
                    details=_bounded_details(preflight.stdout, preflight.stderr),
                )

            try:
                installed = _install_requirement(
                    QUALIFIED_ECHO_VEIL_REQUIREMENT,
                    installer=selected_installer,
                    executable=python,
                    runner=runner,
                    cwd=root,
                )
                if installed.returncode == 0:
                    verified = _verify_installed(
                        executable=python,
                        version=QUALIFIED_ECHO_VEIL_VERSION,
                        commit=QUALIFIED_ECHO_VEIL_COMMIT,
                        runner=runner,
                        cwd=root,
                    )
                else:
                    verified = installed
            except (OSError, subprocess.TimeoutExpired) as exc:
                installed = None
                verified = None
                failure_details = str(exc)
            else:
                failure_details = _bounded_details(
                    installed.stdout,
                    installed.stderr,
                    verified.stdout,
                    verified.stderr,
                )
            if installed is None or verified is None or verified.returncode != 0:
                rollback_ok, rollback_details = _rollback(
                    before_version=before_version,
                    installer=selected_installer,
                    executable=python,
                    runner=runner,
                    cwd=root,
                )
                return EchoUpdateResult(
                    returncode=1,
                    before_version=before_version,
                    after_version=before_version if rollback_ok else None,
                    changed=False,
                    message=(
                        "Echo Veil runtime verification failed after installation; "
                        + (
                            "the previous qualified state was restored."
                            if rollback_ok
                            else "automatic rollback did not verify."
                        )
                    ),
                    details=_bounded_details(failure_details, rollback_details),
                    rollback_attempted=True,
                    rollback_succeeded=rollback_ok,
                )
    except OSError as exc:
        return EchoUpdateResult(
            returncode=1,
            before_version=before_version,
            after_version=before_version,
            changed=False,
            message="Echo Veil update staging directory could not be created.",
            details=str(exc),
        )

    changed = bool(
        before_version != QUALIFIED_ECHO_VEIL_VERSION
        or current.source_commit != QUALIFIED_ECHO_VEIL_COMMIT
        or current.installation_kind != "vcs-pinned"
    )
    return EchoUpdateResult(
        returncode=0,
        before_version=before_version,
        after_version=QUALIFIED_ECHO_VEIL_VERSION,
        changed=changed,
        message=(
            "Installed Algo-qualified Echo Veil "
            f"{QUALIFIED_ECHO_VEIL_VERSION} "
            f"({QUALIFIED_ECHO_VEIL_COMMIT[:12]}). Fresh-process package, "
            "provenance, encryption, lifecycle, scope, doctor, and restart "
            "verification passed. Restart Algo CLI to load it."
        ),
    )


def classify_echo_request(text: str) -> str | None:
    """Recognize status/update intent without sending package maintenance to an LLM."""

    clean = " ".join(str(text or "").strip().casefold().split())
    if not re.search(r"\becho(?:[\s-]+veil)?\b", clean):
        return None
    direct_update = re.fullmatch(
        r"(?:please\s+)?(?:(?:update|upgrade|install|repair)\s+"
        r"echo(?:[\s-]+veil)?|echo(?:[\s-]+veil)?\s+"
        r"(?:update|upgrade|install|repair))(?:\s+(?:now|please))?[.!]?",
        clean,
    )
    if direct_update:
        return "update"
    if re.search(
        r"\b(status|version|review|inspect|check|latest|current|updated|up[\s-]*to[\s-]*date)\b",
        clean,
    ):
        return "status"
    return None
