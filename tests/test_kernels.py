from __future__ import annotations

from algo_cli.config import Config


def test_kernel_manifest_loads() -> None:
    from algo_cli.kernels.manifest import KernelSpec, list_kernels

    specs = list_kernels()

    assert specs
    assert all(isinstance(spec, KernelSpec) for spec in specs)


def test_kernel_names_include_initial_registry_entries() -> None:
    from algo_cli.kernels.manifest import kernel_names

    names = kernel_names()

    assert "benchmark" in names
    assert "repo-intelligence" in names


def test_kernel_names_include_harness_runtime_acceleration_entries() -> None:
    from algo_cli.kernels.manifest import kernel_names

    names = set(kernel_names())

    assert {
        "harness-lexical-index",
        "harness-vector-ann",
        "harness-fusion-ranker",
        "harness-context-pack",
        "harness-incremental-index",
        "harness-rerank",
    }.issubset(names)


def test_harness_runtime_kernels_have_distinct_stage_actions() -> None:
    from algo_cli.kernels.manifest import list_kernels

    specs = {spec.name: spec for spec in list_kernels()}

    assert set(specs["harness-lexical-index"].actions) == {
        "harness.lexical.build",
        "harness.lexical.query",
    }
    assert set(specs["harness-vector-ann"].actions) == {
        "harness.vector.build",
        "harness.vector.query",
    }
    assert set(specs["harness-fusion-ranker"].actions) == {
        "harness.fusion.lexical_rank",
        "harness.fusion.rank",
        "harness.fusion.explain",
    }
    assert set(specs["harness-context-pack"].actions) == {
        "harness.context.pack",
        "harness.context.explain",
    }
    assert set(specs["harness-incremental-index"].actions) == {
        "harness.index.refresh_changed",
        "harness.index.status",
    }
    assert set(specs["harness-rerank"].actions) == {
        "harness.rerank.candidates",
        "harness.rerank.explain",
    }


def test_kernel_actions_are_unique_across_registry() -> None:
    from algo_cli.kernels.manifest import list_kernels

    actions = [action for spec in list_kernels() for action in spec.actions]

    assert len(actions) == len(set(actions))


def test_get_kernel_benchmark_returns_spec() -> None:
    from algo_cli.kernels.manifest import get_kernel

    spec = get_kernel("benchmark")

    assert spec is not None
    assert spec.name == "benchmark"
    assert "agent_benchmark" in "\n".join(spec.modules)


def test_every_kernel_spec_has_required_public_fields() -> None:
    from algo_cli.kernels.manifest import list_kernels

    for spec in list_kernels():
        assert spec.name
        assert spec.description
        assert spec.modules
        assert spec.safety_level
        assert spec.status


def test_all_kernel_modules_and_slash_routes_pass_live_audit() -> None:
    from algo_cli.kernels.manifest import audit_kernels, list_kernels

    audits = audit_kernels()

    assert len(audits) == len(list_kernels())
    assert all(audit.health != "blocked" for audit in audits)
    assert all(not audit.issues for audit in audits)


def test_active_kernel_actions_have_explicit_action_specs() -> None:
    from algo_cli.action_registry import get_action_spec
    from algo_cli.kernels.manifest import audit_kernels, list_kernels

    active = [spec for spec in list_kernels() if spec.status == "active"]

    assert active
    for kernel in active:
        audit = audit_kernels(kernel.name)[0]
        assert audit.health == "ready"
        assert audit.registered_actions == audit.total_actions
        for action in kernel.actions:
            assert get_action_spec(action).kind == "kernel"


def test_kernel_list_slash_includes_known_kernels(monkeypatch) -> None:
    from algo_cli import main as main_module

    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/kernel list", Config(), None)

    assert handled is True
    output = "\n".join(printed)
    assert "Kernels:" in output
    assert "benchmark" in output
    assert "repo-intelligence" in output


def test_kernel_show_benchmark_includes_details(monkeypatch) -> None:
    from algo_cli import main as main_module

    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/kernel show benchmark", Config(), None)

    assert handled is True
    output = "\n".join(printed)
    assert "Kernel: benchmark" in output
    assert "Modules:" in output
    assert "algo_cli.intelligence.agent_benchmark" in output
    assert "Actions:" in output


def test_kernel_check_reports_full_and_single_kernel_readiness(monkeypatch) -> None:
    from algo_cli import main as main_module
    from algo_cli.kernels.manifest import list_kernels

    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/kernel check", Config(), None)
    handled_single, _client = main_module.handle_command(
        "/kernel check agent-runtime", Config(), None
    )

    assert handled is True
    assert handled_single is True
    output = "\n".join(printed)
    assert f"{len(list_kernels())} checked" in output
    assert "0 blocked" in output
    assert "agent-runtime: ready" in output
    assert "4/4 registered actions" in output


def test_kernel_actions_are_registered_and_read_only() -> None:
    from algo_cli.action_registry import get_action_spec, list_action_specs
    from algo_cli.tool_runtime import session_command_requires_approval

    names = {spec.name for spec in list_action_specs()}

    assert "kernel.list" in names
    assert "kernel.show" in names
    assert get_action_spec("kernel.list").requires_approval is False
    assert get_action_spec("kernel.show").requires_approval is False
    assert session_command_requires_approval("/kernel list") is False
    assert session_command_requires_approval("/kernel show benchmark") is False


# ---------------------------------------------------------------------------
# Track H Phase 1 kernels
# ---------------------------------------------------------------------------


def test_track_h_phase1_kernels_are_registered() -> None:
    from algo_cli.kernels.manifest import kernel_names

    names = set(kernel_names())

    assert {
        "echo-fidelity-guard",
        "numeric-clamp-guard",
        "ema-tuning",
        "bonferroni-guard",
        "pre-push-gate",
    }.issubset(names)


def test_track_h_phase1_kernel_actions_are_unique() -> None:
    from algo_cli.kernels.manifest import list_kernels

    track_h_names = {
        "echo-fidelity-guard",
        "numeric-clamp-guard",
        "ema-tuning",
        "bonferroni-guard",
        "pre-push-gate",
    }
    specs = [spec for spec in list_kernels() if spec.name in track_h_names]
    actions = [action for spec in specs for action in spec.actions]

    assert len(actions) == len(set(actions))


def test_track_h_phase1_kernel_modules_are_importable() -> None:
    from algo_cli.kernels.manifest import list_kernels

    track_h_names = {
        "echo-fidelity-guard",
        "numeric-clamp-guard",
        "ema-tuning",
        "bonferroni-guard",
        "pre-push-gate",
    }
    for spec in list_kernels():
        if spec.name not in track_h_names:
            continue
        for module_path in spec.modules:
            __import__(module_path)


def test_track_h_phase1_kernel_show_echo_fidelity(monkeypatch) -> None:
    from algo_cli import main as main_module

    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command(
        "/kernel show echo-fidelity-guard", Config(), None
    )

    assert handled is True
    output = "\n".join(printed)
    assert "echo-fidelity-guard" in output
    assert "guard.echo_fidelity" in output


# ---------------------------------------------------------------------------
# Track H Phase 2 kernels
# ---------------------------------------------------------------------------


def test_track_h_phase2_kernels_are_registered() -> None:
    from algo_cli.kernels.manifest import kernel_names

    names = set(kernel_names())

    assert {
        "finding-record",
        "retraction-ledger",
        "discovery-event-log",
        "artifact-binding",
        "checkpoint-resume",
        "symmetric-verify",
        "circuit-breaker",
        "negative-controls",
        "stat-stability-guard",
        "context-adaptive",
        "output-normalize",
        "degenerate-detector",
        "delta-report",
        "tiered-access-gate",
        "catalog-verifier",
    }.issubset(names)


def test_track_h_phase2_kernel_actions_are_unique() -> None:
    from algo_cli.kernels.manifest import list_kernels

    track_h_names = {
        "finding-record",
        "retraction-ledger",
        "discovery-event-log",
        "artifact-binding",
        "checkpoint-resume",
        "symmetric-verify",
        "circuit-breaker",
        "negative-controls",
        "stat-stability-guard",
        "context-adaptive",
        "output-normalize",
        "degenerate-detector",
        "delta-report",
        "tiered-access-gate",
        "catalog-verifier",
    }
    specs = [spec for spec in list_kernels() if spec.name in track_h_names]
    actions = [action for spec in specs for action in spec.actions]

    assert len(actions) == len(set(actions))


def test_track_h_phase2_kernel_modules_are_importable() -> None:
    from algo_cli.kernels.manifest import list_kernels

    track_h_names = {
        "finding-record",
        "retraction-ledger",
        "discovery-event-log",
        "artifact-binding",
        "checkpoint-resume",
        "symmetric-verify",
        "circuit-breaker",
        "negative-controls",
        "stat-stability-guard",
        "context-adaptive",
        "output-normalize",
        "degenerate-detector",
        "delta-report",
        "tiered-access-gate",
        "catalog-verifier",
    }
    for spec in list_kernels():
        if spec.name not in track_h_names:
            continue
        for module_path in spec.modules:
            __import__(module_path)


# ---------------------------------------------------------------------------
# Track H — Phase 3: LLM-integrated kernels (H5, H8, H12, H15, H18, H20, H25, H26)
# ---------------------------------------------------------------------------

PHASE3_KERNELS = {
    "lesson-catalog-propose",
    "adversarial-audit",
    "multi-model-score",
    "llm-fallback-chain",
    "dual-layer-validate",
    "falsification-suite",
    "consortium-synthesis",
    "multi-tier-grade",
}


def test_track_h_phase3_kernels_are_registered() -> None:
    from algo_cli.kernels.manifest import kernel_names

    names = kernel_names()
    for name in PHASE3_KERNELS:
        assert name in names, f"Phase 3 kernel '{name}' not registered"


def test_track_h_phase3_kernel_actions_are_unique() -> None:
    from algo_cli.kernels.manifest import list_kernels

    phase3_actions = set()
    for spec in list_kernels():
        if spec.name in PHASE3_KERNELS:
            for action in spec.actions:
                assert action not in phase3_actions, f"Duplicate action: {action}"
                phase3_actions.add(action)


def test_track_h_phase3_kernel_modules_are_importable() -> None:
    from algo_cli.kernels.manifest import list_kernels

    for spec in list_kernels():
        if spec.name not in PHASE3_KERNELS:
            continue
        for module_path in spec.modules:
            __import__(module_path)


# ---------------------------------------------------------------------------
# Small-context runtime kernel
# ---------------------------------------------------------------------------


def test_small_context_kernel_is_registered_and_importable() -> None:
    from algo_cli.kernels.manifest import get_kernel

    spec = get_kernel("small-context-ledger")

    assert spec is not None
    assert "algo_cli.small_context" in spec.modules
    assert "small_context.ledger.preview" in spec.actions
    for module_path in spec.modules:
        __import__(module_path)
