"""Isolation and reporting for end-to-end scenarios (see README.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenarios.scenario_harness import (
    REAL_ALGO_DIR,
    SCENARIO_METRICS,
    ExternalCallGuard,
    ScenarioRunner,
    format_metrics_table,
)


@pytest.fixture(autouse=True)
def _scenario_isolation(monkeypatch, tmp_path):
    """Keep scenarios away from the real home, keychain receipt stores and ~/.algo_cli."""
    from algo_cli import config as config_module
    from algo_cli import grace_key_store, irene_privacy_views
    from algo_cli.grace_key_store import StaticKeyStore
    from algo_cli.grace_memory_receipts import ElsieReceiptAuthority
    from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    assert not Path(config_module.CONFIG_DIR).resolve().is_relative_to(REAL_ALGO_DIR)

    monkeypatch.setattr(irene_privacy_views, "_PRIVACY_KEY", b"s" * 32)
    authority = ElsieReceiptAuthority.from_key_store(store=StaticKeyStore({PRIVACY_KEY_LABEL: b"s" * 32}))
    monkeypatch.setattr(ElsieReceiptAuthority, "from_key_store", classmethod(lambda _cls, **_kw: authority))
    monkeypatch.setattr(ElsieReceiptAuthority, "from_existing_key_store", classmethod(lambda _cls, **_kw: authority))

    def forbid_live_store(*_args, **_kwargs):
        raise AssertionError("scenarios must not open live keychain receipt stores")

    monkeypatch.setattr(grace_key_store, "KeyringKeyStore", forbid_live_store)
    monkeypatch.setattr(grace_key_store, "GraceReceiptAnchorStore", forbid_live_store)
    yield
    assert not Path(config_module.CONFIG_DIR).resolve().is_relative_to(REAL_ALGO_DIR)


@pytest.fixture(autouse=True)
def external_guard(monkeypatch):
    """Fail any scenario that reaches xurl, Google, TypeSafe, Ollama web or the network."""
    guard = ExternalCallGuard()
    guard.install(monkeypatch)
    yield guard
    guard.assert_clean()


@pytest.fixture
def scenario(monkeypatch, tmp_path, request, external_guard) -> ScenarioRunner:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = ScenarioRunner(monkeypatch, workspace, request.node.name, guard=external_guard)
    yield runner
    runner.close()


def pytest_terminal_summary(terminalreporter):
    if not SCENARIO_METRICS:
        return
    terminalreporter.section("scenario metrics")
    terminalreporter.write_line(format_metrics_table(SCENARIO_METRICS))
