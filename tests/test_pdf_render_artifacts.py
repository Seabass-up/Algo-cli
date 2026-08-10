"""Encrypted lifecycle, typed-consumer, and policy tests for PDF renders."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from algo_cli import config, main, nathan_runtime, tools
from algo_cli.config import Config
from algo_cli.alice_artifact_store import ArtifactPolicy, EncryptedArtifactStore
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.marcus_authority import (
    CURATED_TOOL_POLICIES,
    Capability,
    ConfirmationMode,
    DataClass,
    EffectClass,
    IdempotencyClass,
)


_MASTER_KEY = b"pdf-render-static-test-master-key"[:32]


class _MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakePixmap:
    def __init__(
        self,
        payload: bytes = tools._PNG_SIGNATURE + b"PAGE-ONE",
        *,
        width: int = 10,
        height: int = 10,
    ) -> None:
        self._payload = payload
        self.width = width
        self.height = height

    def tobytes(self, kind: str) -> bytes:
        assert kind == "png"
        return self._payload


class _FakePage:
    def __init__(self, pixmap: _FakePixmap) -> None:
        self._pixmap = pixmap

    def get_pixmap(self, *, matrix, alpha: bool):
        assert matrix is not None
        assert alpha is False
        return self._pixmap


class _FakeDocument:
    def __init__(
        self,
        pages: list[_FakePixmap],
        *,
        fail_index: int | None = None,
    ) -> None:
        self._pages = pages
        self._fail_index = fail_index

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def __len__(self) -> int:
        return len(self._pages)

    def load_page(self, index: int) -> _FakePage:
        if index == self._fail_index:
            raise RuntimeError("injected page render failure")
        return _FakePage(self._pages[index])


class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def _install_fake_fitz(
    monkeypatch,
    *,
    pages: list[_FakePixmap] | None = None,
    fail_index: int | None = None,
) -> None:
    selected = (
        pages
        if pages is not None
        else [
            _FakePixmap(tools._PNG_SIGNATURE + b"PAGE-ONE"),
            _FakePixmap(tools._PNG_SIGNATURE + b"PAGE-TWO"),
            _FakePixmap(tools._PNG_SIGNATURE + b"PAGE-THREE"),
        ]
    )
    fake = SimpleNamespace(
        open=lambda _path: _FakeDocument(selected, fail_index=fail_index),
        Matrix=lambda x, y: (x, y),
    )
    monkeypatch.setitem(__import__("sys").modules, "fitz", fake)


def _static_store(
    root: Path,
    clock: _MutableClock,
    *,
    policy: ArtifactPolicy = tools.PDF_RENDER_ARTIFACT_POLICY,
) -> EncryptedArtifactStore:
    return EncryptedArtifactStore(
        root,
        policy=policy,
        key_store=StaticKeyStore({"alice-artifact-master-v1": _MASTER_KEY}),
        clock=clock,
    )


def _artifact_file(root: Path, rendered: dict, page_index: int = 0) -> Path:
    artifact_id = rendered["pages"][page_index]["artifact_uri"].rsplit("/", 1)[-1]
    return root / "runs" / rendered["artifact_id"] / "artifacts" / f"{artifact_id}.alice"


def _active_run_directories(root: Path) -> list[Path]:
    return [path for path in (root / "runs").iterdir() if path.is_dir() and len(path.name) == 32]


@pytest.fixture
def pdf_artifact_environment(tmp_path: Path, monkeypatch):
    root = tmp_path / "pdf-artifacts"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    clock = _MutableClock()
    store = _static_store(root, clock)
    monkeypatch.setattr(tools, "PDF_RENDER_ARTIFACT_ROOT", root)
    monkeypatch.setattr(tools, "_PDF_RENDER_STORE", store)
    monkeypatch.setattr(tools, "_schedule_pdf_render_cleanup", lambda *_args: None)
    monkeypatch.setattr(tools, "load_runtime_env", lambda **_kwargs: None)
    with tools._PDF_RENDER_CLEANUP_LOCK:
        tools._PDF_RENDER_SESSIONS.clear()
        tools._PDF_RENDER_TIMERS.clear()
    yield source, root, store, clock
    with tools._PDF_RENDER_CLEANUP_LOCK:
        timers = tuple(tools._PDF_RENDER_TIMERS.values())
        tools._PDF_RENDER_TIMERS.clear()
        tools._PDF_RENDER_SESSIONS.clear()
    for timer in timers:
        timer.cancel()


def test_render_is_random_private_ciphertext_only_and_explicitly_cleanable(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, root, _store, _clock = pdf_artifact_environment
    page_two = tools._PNG_SIGNATURE + b"SENSITIVE-PAGE-TWO"
    page_three = tools._PNG_SIGNATURE + b"SENSITIVE-PAGE-THREE"
    _install_fake_fitz(
        monkeypatch,
        pages=[
            _FakePixmap(tools._PNG_SIGNATURE + b"PAGE-ONE"),
            _FakePixmap(page_two),
            _FakePixmap(page_three),
        ],
    )

    first = json.loads(
        tools.render_pdf_pages(
            str(source),
            start_page=2,
            max_pages=2,
            scale=1.5,
            ttl_seconds=120,
        )
    )
    second = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120))

    assert first["artifact_id"] != second["artifact_id"]
    assert first["rendered_pages"] == 2
    assert [row["page_number"] for row in first["pages"]] == [2, 3]
    assert "paths" not in first
    assert "artifact_directory" not in first
    assert first["lifecycle"]["classification"] == ("explicit_encrypted_operational_artifact")
    assert first["lifecycle"]["at_rest"] == "alice_aes_256_gcm_ciphertext_only"
    assert first["lifecycle"]["cleanup_tool"] == "cleanup_pdf_render_artifact"

    run_directory = root / "runs" / first["artifact_id"]
    persisted = [path.read_bytes() for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    persisted_bytes = b"".join(persisted)
    assert page_two not in persisted_bytes
    assert page_three not in persisted_bytes
    assert hashlib.sha256(page_two).hexdigest().encode("ascii") not in persisted_bytes
    assert str(source).encode() not in persisted_bytes
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(run_directory.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in run_directory.rglob("*") if path.is_file())

    wrong = "hmac-sha256:" + "0" * 64
    refused = json.loads(tools.cleanup_pdf_render_artifact(first["artifact_id"], wrong))
    assert refused["status"] == "error"
    assert run_directory.exists()

    removed = json.loads(
        tools.cleanup_pdf_render_artifact(
            first["artifact_id"],
            first["artifact_receipt"],
        )
    )
    absent = json.loads(
        tools.cleanup_pdf_render_artifact(
            first["artifact_id"],
            first["artifact_receipt"],
        )
    )
    assert removed["status"] == "removed"
    assert removed["ciphertext_files_removed"] == 2
    assert absent["status"] == "already_absent"
    assert not run_directory.exists()


def test_typed_render_to_vision_uses_bytes_and_exact_source_page(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, _root, _store, _clock = pdf_artifact_environment
    selected = tools._PNG_SIGNATURE + b"ONLY-PAGE-TWO"
    _install_fake_fitz(
        monkeypatch,
        pages=[
            _FakePixmap(tools._PNG_SIGNATURE + b"PAGE-ONE"),
            _FakePixmap(selected),
        ],
    )
    rendered = json.loads(
        tools.render_pdf_pages(
            str(source),
            start_page=2,
            max_pages=1,
            ttl_seconds=120,
        )
    )
    calls: list[dict] = []

    class _VisionClient:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message=SimpleNamespace(content="typed image seen"))

    monkeypatch.setattr(tools, "active_ollama_client", lambda: _VisionClient())

    result = tools.vision_describe(
        artifact_id=rendered["artifact_id"],
        artifact_page=2,
        artifact_receipt=rendered["artifact_receipt"],
    )

    assert result == "typed image seen"
    assert calls[0]["messages"][0]["images"] == [selected]
    assert not list(source.parent.glob("*.png"))
    assert "require empty image_path" in tools.vision_describe(
        image_path="/tmp/forged.png",
        artifact_id=rendered["artifact_id"],
        artifact_page=2,
        artifact_receipt=rendered["artifact_receipt"],
    )


def test_echo_required_runtime_consumes_typed_protected_artifact_end_to_end(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, _root, _store, clock = pdf_artifact_environment
    protected = source.parent / ".algo_cli"
    artifact_root = protected / "private" / "pdf-render"
    workspace = source.parent / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    monkeypatch.setattr(tools, "PDF_RENDER_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(tools, "_PDF_RENDER_STORE", _static_store(artifact_root, clock))
    _install_fake_fitz(monkeypatch)
    rendered = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120))
    calls: list[dict] = []

    class _VisionClient:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message=SimpleNamespace(content="echo typed image seen"))

    monkeypatch.setattr(tools, "active_ollama_client", lambda: _VisionClient())
    monkeypatch.setitem(nathan_runtime.TOOL_MAP, "vision_describe", tools.vision_describe)
    cfg = Config(
        cwd=str(workspace),
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    result = nathan_runtime.run_tool(
        "vision_describe",
        {
            "image_path": "",
            "artifact_id": rendered["artifact_id"],
            "artifact_page": 1,
            "artifact_receipt": rendered["artifact_receipt"],
        },
        cfg,
    )

    assert result == "echo typed image seen"
    assert calls[0]["messages"][0]["images"][0].startswith(tools._PNG_SIGNATURE)
    assert "cwd" not in nathan_runtime.tool_runtime_args(
        "vision_describe",
        {
            "image_path": "",
            "artifact_id": rendered["artifact_id"],
            "artifact_page": 1,
            "artifact_receipt": rendered["artifact_receipt"],
        },
        cfg,
    )


def test_typed_consumer_rejects_forged_swapped_ungranted_and_expired_refs(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, _root, _store, clock = pdf_artifact_environment
    _install_fake_fitz(monkeypatch)
    first = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=60))
    second = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=60))
    monkeypatch.setattr(
        tools,
        "active_ollama_client",
        lambda: pytest.fail("invalid artifact reached the model"),
    )
    generic_error = "Error: PDF render artifact is invalid, expired, or unavailable."

    assert (
        tools.vision_describe(
            artifact_id=first["artifact_id"],
            artifact_page=1,
            artifact_receipt="hmac-sha256:" + "0" * 64,
        )
        == generic_error
    )
    assert (
        tools.vision_describe(
            artifact_id=second["artifact_id"],
            artifact_page=1,
            artifact_receipt=first["artifact_receipt"],
        )
        == generic_error
    )
    assert (
        tools.vision_describe(
            artifact_id=first["artifact_id"],
            artifact_page=2,
            artifact_receipt=first["artifact_receipt"],
        )
        == generic_error
    )

    clock.value = float(first["expires_at"])
    assert (
        tools.vision_describe(
            artifact_id=first["artifact_id"],
            artifact_page=1,
            artifact_receipt=first["artifact_receipt"],
        )
        == generic_error
    )


@pytest.mark.parametrize("replacement", ["same_size_tamper", "symlink"])
def test_typed_consumer_rejects_ciphertext_substitution_without_following(
    pdf_artifact_environment,
    monkeypatch,
    replacement: str,
) -> None:
    source, root, _store, _clock = pdf_artifact_environment
    _install_fake_fitz(monkeypatch)
    rendered = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120))
    artifact = _artifact_file(root, rendered)
    external = source.parent / "outside.bin"
    external.write_bytes(b"OUTSIDE-CANARY")

    if replacement == "same_size_tamper":
        changed = bytearray(artifact.read_bytes())
        changed[len(changed) // 2] ^= 1
        artifact.write_bytes(bytes(changed))
        artifact.chmod(0o600)
    else:
        artifact.unlink()
        try:
            artifact.symlink_to(external)
        except OSError:
            pytest.skip("symlinks unavailable")

    monkeypatch.setattr(
        tools,
        "active_ollama_client",
        lambda: pytest.fail("tampered artifact reached the model"),
    )
    result = tools.vision_describe(
        artifact_id=rendered["artifact_id"],
        artifact_page=1,
        artifact_receipt=rendered["artifact_receipt"],
    )

    assert result == "Error: PDF render artifact is invalid, expired, or unavailable."
    assert external.read_bytes() == b"OUTSIDE-CANARY"


def test_bounds_and_partial_failure_leave_no_active_ciphertext(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, root, _store, _clock = pdf_artifact_environment
    _install_fake_fitz(monkeypatch)

    assert "max_pages must be between" in tools.render_pdf_pages(str(source), max_pages=tools.MAX_RENDER_PDF_PAGES + 1)
    assert "scale must be finite" in tools.render_pdf_pages(str(source), scale=float("inf"))
    assert _active_run_directories(root) == []


@pytest.mark.parametrize("failure_stage", ["schedule", "response"])
def test_post_registration_failure_revokes_map_timer_and_alice_run(
    pdf_artifact_environment,
    monkeypatch,
    failure_stage: str,
) -> None:
    source, root, _store, _clock = pdf_artifact_environment
    _install_fake_fitz(monkeypatch, pages=[_FakePixmap()])
    captured_ids: list[str] = []
    timers: list[_FakeTimer] = []

    def schedule(artifact_id: str, _expires_at: float) -> None:
        timer = _FakeTimer()
        captured_ids.append(artifact_id)
        timers.append(timer)
        with tools._PDF_RENDER_CLEANUP_LOCK:
            tools._PDF_RENDER_TIMERS[artifact_id] = timer  # type: ignore[assignment]
        if failure_stage == "schedule":
            raise RuntimeError("injected schedule failure")

    monkeypatch.setattr(tools, "_schedule_pdf_render_cleanup", schedule)
    if failure_stage == "response":
        monkeypatch.setattr(
            tools,
            "_pdf_render_response_json",
            lambda _payload: (_ for _ in ()).throw(RuntimeError("injected response serialization failure")),
        )

    result = tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120)

    assert result.startswith("Error rendering PDF pages")
    assert captured_ids
    assert all(timer.cancelled for timer in timers)
    assert captured_ids[0] not in tools._PDF_RENDER_SESSIONS
    assert captured_ids[0] not in tools._PDF_RENDER_TIMERS
    assert _active_run_directories(root) == []

    _install_fake_fitz(monkeypatch, pages=[_FakePixmap(width=3, height=2)])
    monkeypatch.setattr(tools, "MAX_RENDER_PDF_PIXELS", 4)
    assert "exceeds the pixel limit" in tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120)
    assert _active_run_directories(root) == []

    oversized = tools._PNG_SIGNATURE + b"X"
    _install_fake_fitz(monkeypatch, pages=[_FakePixmap(oversized)])
    monkeypatch.setattr(tools, "MAX_RENDER_PDF_PIXELS", 40_000_000)
    monkeypatch.setattr(tools, "MAX_RENDER_PDF_PAGE_BYTES", len(oversized) - 1)
    assert "PNG/byte limit" in tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=120)
    assert _active_run_directories(root) == []

    monkeypatch.setattr(tools, "MAX_RENDER_PDF_PAGE_BYTES", 32 * 1024 * 1024)
    _install_fake_fitz(monkeypatch, fail_index=1)
    assert "injected page render failure" in tools.render_pdf_pages(str(source), max_pages=2, ttl_seconds=120)
    assert _active_run_directories(root) == []


def test_expiry_revocation_and_partial_cleanup_are_deterministic(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, root, _store, clock = pdf_artifact_environment
    _install_fake_fitz(monkeypatch)
    rendered = json.loads(tools.render_pdf_pages(str(source), max_pages=1, ttl_seconds=60))
    clock.value = float(rendered["expires_at"])

    removed, count = tools._revoke_pdf_render_session(rendered["artifact_id"])

    assert removed is True
    assert count == 1
    assert _active_run_directories(root) == []
    assert rendered["artifact_id"] not in tools._PDF_RENDER_SESSIONS


def test_alice_lease_enforces_pdf_run_quota_under_concurrent_renders(
    pdf_artifact_environment,
    monkeypatch,
) -> None:
    source, root, _store, clock = pdf_artifact_environment
    policy = ArtifactPolicy(
        max_artifact_bytes=1024,
        max_run_bytes=1024,
        max_run_disk_bytes=8192,
        max_total_bytes=1024,
        max_total_disk_bytes=8192,
        max_artifacts_per_run=1,
        max_runs=1,
        default_ttl_seconds=120,
        max_ttl_seconds=120,
    )
    monkeypatch.setattr(tools, "_PDF_RENDER_STORE", _static_store(root, clock, policy=policy))
    _install_fake_fitz(monkeypatch, pages=[_FakePixmap()])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: tools.render_pdf_pages(
                    str(source),
                    max_pages=1,
                    ttl_seconds=120,
                ),
                range(2),
            )
        )

    successes = [json.loads(result) for result in results if result.startswith("{")]
    failures = [result for result in results if result.startswith("Error")]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "quota" in failures[0]
    assert len(_active_run_directories(root)) == 1


def test_render_and_cleanup_have_truthful_sensitive_action_time_policies() -> None:
    from algo_cli.action_registry import ACTION_SPECS, effective_action_specs

    render = CURATED_TOOL_POLICIES["render_pdf_pages"]
    cleanup = CURATED_TOOL_POLICIES["cleanup_pdf_render_artifact"]

    assert render.effect_class is EffectClass.LOCAL_MUTATION
    assert render.confirmation_mode is ConfirmationMode.ACTION_TIME
    assert render.idempotency is IdempotencyClass.AT_MOST_ONCE
    assert Capability.WRITE in render.capabilities
    assert DataClass.SENSITIVE in render.data_classes
    assert render.suppress_logs is True
    assert cleanup.effect_class is EffectClass.DESTRUCTIVE
    assert cleanup.confirmation_mode is ConfirmationMode.ACTION_TIME
    assert cleanup.idempotency is IdempotencyClass.IDEMPOTENT
    assert Capability.DESTRUCTIVE in cleanup.capabilities
    assert DataClass.SENSITIVE in cleanup.data_classes
    assert cleanup.suppress_logs is True
    assert CURATED_TOOL_POLICIES["vision_describe"].suppress_logs is True
    assert tools.render_pdf_pages not in main.READ_ONLY_TOOLS
    assert tools.render_pdf_pages in tools.ALL_TOOLS
    assert tools.cleanup_pdf_render_artifact in tools.ALL_TOOLS
    explicit = {spec.name for spec in ACTION_SPECS}
    assert {"render_pdf_pages", "cleanup_pdf_render_artifact"} <= explicit
    registered = {spec.name for spec in effective_action_specs() if spec.kind == "tool"}
    runtime_mutations = {name for name in tools.TOOL_MAP if CURATED_TOOL_POLICIES[name].mutates_state}
    assert runtime_mutations <= registered
    focused = json.loads(tools.available_actions("files"))["focused"]
    assert "cleanup_pdf_render_artifact" in focused["model_callable_tools"]["files"]
