from __future__ import annotations

import copy

from algo_cli import decision_coordinator as c, decision_layer as d, jev_kernel
from algo_cli.config import Config


def jev_fixture(monkeypatch, tmp_path):
    cli = tmp_path / "jev-workflows"
    cli.write_text("fixture")
    cfg = Config(jev_kernel_enabled=True, jev_kernel_cli=str(cli))
    calls = []
    monkeypatch.setattr(jev_kernel, "kernel_status", lambda cfg: calls.append(True) or {
        "ok": True, "enabled": cfg.jev_kernel_enabled, "model": d.JevProvider.model})
    return cfg, calls, cli


def test_routing_readiness_reuses_only_successful_companion_metadata(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    cfg, calls, _cli = jev_fixture(monkeypatch, tmp_path)
    first = r.status_for_routing(cfg, d.JevProvider(cfg), {})
    second = r.status_for_routing(cfg, d.JevProvider(cfg), {})
    assert calls == [True]
    assert first["readiness_cache"]["hit"] is False and second["readiness_cache"]["hit"] is True
    second["enabled"] = False
    assert r.status_for_routing(cfg, d.JevProvider(cfg), {})["enabled"] is True


def test_readiness_expiry_disable_and_configuration_change(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    cfg, calls, _cli = jev_fixture(monkeypatch, tmp_path)
    now = [0.0]
    monkeypatch.setattr(r.time, "monotonic", lambda: now[0])
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    now[0] = 6.0
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    cfg.jev_kernel_enabled = False
    assert r.status_for_routing(cfg, d.JevProvider(cfg), {})["enabled"] is False
    cfg.decision_readiness_ttl_seconds = 0
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    assert len(calls) == 5


def test_readiness_cli_replacement_and_model_identity_invalidate(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    cfg, calls, cli = jev_fixture(monkeypatch, tmp_path)
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    cli.write_text("changed fixture size")
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    provider = d.JevProvider(cfg)
    provider.model = "fixture-new-revision"
    r.status_for_routing(cfg, provider, {})
    assert len(calls) == 3


def test_raw_cli_configuration_and_permissions_invalidate_cache(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    cfg, calls, cli = jev_fixture(monkeypatch, tmp_path)
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    cli.chmod(0o700)
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    monkeypatch.chdir(tmp_path)
    cfg.jev_kernel_cli = cli.name
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    assert len(calls) == 3


def test_failed_readiness_and_separate_configs_do_not_share_cache(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    cfg, calls, cli = jev_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(jev_kernel, "kernel_status", lambda cfg: calls.append(True) or {"ok": False})
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    assert len(calls) == 2
    monkeypatch.setattr(jev_kernel, "kernel_status", lambda cfg: calls.append(True) or {"ok": True})
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    other = Config(jev_kernel_enabled=True, jev_kernel_cli=str(cli))
    r.status_for_routing(other, d.JevProvider(other), {})
    assert len(calls) == 4


def test_cache_shared_with_checkpoint_copy_but_not_serialized(monkeypatch, tmp_path):
    from algo_cli import decision_readiness as r
    from algo_cli.decision_checkpoints import TaskCheckpoints
    cfg, calls, _cli = jev_fixture(monkeypatch, tmp_path)
    TaskCheckpoints(cfg, "Review source", {"decision_decide"}, lambda *a: None)
    r.status_for_routing(copy.copy(cfg), d.JevProvider(cfg), {})
    r.status_for_routing(cfg, d.JevProvider(cfg), {})
    assert calls == [True]
    cfg.save()
    from algo_cli.config import CONFIG_FILE
    assert "_decision_readiness_cache" not in CONFIG_FILE.read_text()


def ollama_fixture(monkeypatch):
    calls = []
    digest = ["a" * 64]
    def http(base, route, payload, timeout):
        calls.append((route, payload))
        if route == "/api/version":
            return {"version": "0.35.1"}
        if route == "/api/tags":
            return {"models": [{"name": name + ":latest", "digest": digest[0]}
                               for name in ("clef", "clef-flash")]}
        if route == "/api/ps":
            return {"models": []}
        if route == "/api/show":
            return {"capabilities": ["decision"], "details": {"format": "gguf"}}
        raise AssertionError(route)
    monkeypatch.setattr(d, "_http_json", http)
    return calls, digest


def test_ollama_discovery_shares_one_snapshot_only_within_catalog(monkeypatch):
    calls, digest = ollama_fixture(monkeypatch)
    cfg = Config(decision_router_candidates=["clef", "clef-flash"])
    question = {"q": {"type": "noul", "instructions": "Is evidence present?"}}
    eligible, excluded = c._catalog(cfg, "fixture", question, "", offline=False)
    assert not excluded and len(eligible) == 2
    assert len(calls) == 5
    assert sum(route == "/api/show" for route, _ in calls) == 2
    digest[0] = "b" * 64
    eligible, excluded = c._catalog(cfg, "fixture", question, "", offline=False)
    assert len(calls) == 10 and not excluded
    assert all(row["model_digest"] == "b" * 64 for row in eligible.values())


def test_routing_snapshot_never_replaces_pre_and_post_inference_identity_checks(monkeypatch):
    from algo_cli import decision_readiness as r
    calls, digest = ollama_fixture(monkeypatch)
    cfg = Config()
    p = d.OllamaProvider("clef", cfg, {})
    r.status_for_routing(cfg, p, {})
    assert len(calls) == 4
    original = d._http_json
    def infer_http(base, route, payload, timeout):
        if route == "/v1/systemone":
            digest[0] = "b" * 64
            return {"model": "clef:latest", "answers": {}}
        return original(base, route, payload, timeout)
    monkeypatch.setattr(d, "_http_json", infer_http)
    import pytest
    with pytest.raises(d.DecisionError, match="model_changed_during_inference"):
        p.infer("fixture", {"q": {"type": "noul", "instructions": "Is this present?"}}, 1)
    assert len(calls) == 12
