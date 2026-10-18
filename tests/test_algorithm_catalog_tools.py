from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from algo_cli import algorithm_catalog as catalog, config, harness, nathan_runtime, prompt_rules, tools
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action
from algo_cli.marcus_authority import ConfirmationMode, TargetScope, policy_for_action
from algo_cli.nathan_program_runtime import authorization_for_actions
from algo_cli.session_mode import restore_yolo_activation


def _owner_catalog(text="# My catalog\n\n### A1. Exact boundary\nStatus: proposed\n"):
    path = config.CONFIG_DIR / "ALGO.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_paged_owner_catalog_read_is_lossless_and_hash_bound():
    text = "# Owner\n" + "\N{GREEK SMALL LETTER ALPHA}" * 61 + "\nStatus: proposed\n"
    path = _owner_catalog(text)
    pieces, offset = [], 0
    while True:
        result = json.loads(catalog.algorithm_catalog_read(offset=offset, limit=13))
        assert result["sha256"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert result["action_authority"] is False
        assert result["total_chars"] == len(text)
        pieces.append(result["content"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert "".join(pieces) == text


@pytest.mark.parametrize("args", [{"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 32_001}, {"limit": True}])
def test_invalid_page_never_reads(monkeypatch, args):
    monkeypatch.setattr(catalog, "_read", lambda: pytest.fail("invalid input read catalog"))
    assert catalog.algorithm_catalog_read(**args).startswith("Error:")


def test_missing_catalog_never_substitutes_public_template():
    assert "No public template" in catalog.algorithm_catalog_read()
    assert not (config.CONFIG_DIR / "ALGO.md").exists()
    assert catalog.algorithm_catalog_edit("sha256:" + "0" * 64, "old", "new").startswith("Error:")
    assert not (config.CONFIG_DIR / "ALGO.md.lock").exists()


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "directory"])
def test_alias_and_special_catalog_refused_without_touching_sibling(alias):
    secret = config.CONFIG_DIR / "config.json"
    secret.write_text("PRIVATE_CANARY", encoding="utf-8")
    path = config.CONFIG_DIR / "ALGO.md"
    if alias == "directory":
        path.mkdir()
    elif alias == "hardlink":
        os.link(secret, path)
    else:
        try:
            path.symlink_to(secret)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    digest = "sha256:" + hashlib.sha256(b"PRIVATE_CANARY").hexdigest()
    for result in (catalog.algorithm_catalog_read(), catalog.algorithm_catalog_edit(digest, "PRIVATE_CANARY", "oops")):
        assert result.startswith("Error:")
        assert "PRIVATE_CANARY" not in result
    assert secret.read_text() == "PRIVATE_CANARY"


def test_exact_edit_preserves_other_content_and_rejects_stale_retry():
    path = _owner_catalog()
    before = path.read_text()
    read = json.loads(catalog.algorithm_catalog_read())
    result = json.loads(catalog.algorithm_catalog_edit(read["sha256"], "Status: proposed", "Status: planned"))
    assert result["post_write_verified"] is True
    assert result["index_refreshed"] is False
    assert path.read_text() == before.replace("Status: proposed", "Status: planned")
    assert result["sha256"] == json.loads(catalog.algorithm_catalog_read())["sha256"]
    assert "changed since" in catalog.algorithm_catalog_edit(read["sha256"], "Status: planned", "Status: proposed")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("old,new", [("absent", "new"), ("Status", "Status"), ("", "new"), ("Status", "x\x00"), ("Status", "x" * 64_001)],
                         ids=["missing", "no-change", "empty-anchor", "nul", "oversized"])
def test_invalid_edit_does_not_mutate(old, new):
    path = _owner_catalog()
    before = path.read_bytes()
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    assert catalog.algorithm_catalog_edit(digest, old, new).startswith("Error:")
    assert path.read_bytes() == before


def test_nonunique_anchor_is_not_replaced():
    path = _owner_catalog("# repeat repeat\n")
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    assert "exactly once" in catalog.algorithm_catalog_edit(digest, "repeat", "new")
    assert path.read_text() == "# repeat repeat\n"


def test_overlapping_anchor_is_ambiguous():
    path = _owner_catalog("# aaa\n")
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    assert "exactly once" in catalog.algorithm_catalog_edit(digest, "aa", "b")
    assert path.read_text() == "# aaa\n"


def test_oversize_and_invalid_encoding_fail_closed(monkeypatch):
    path = _owner_catalog("# too large\n")
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 5)
    assert catalog.algorithm_catalog_read().startswith("Error:")
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 2_000_000)
    path.write_bytes(b"\xff")
    assert catalog.algorithm_catalog_read().startswith("Error:")


def test_updated_size_bound_refuses_without_writing(monkeypatch):
    path = _owner_catalog("# small\n")
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 10)
    assert "updated catalog exceeds" in catalog.algorithm_catalog_edit(digest, "small", "too large here")
    assert path.read_text() == "# small\n"


def test_catalog_swap_to_secret_during_descriptor_open_is_refused(monkeypatch):
    path = _owner_catalog()
    secret = config.CONFIG_DIR / "env"
    secret.write_text("PRIVATE_CANARY", encoding="utf-8")
    original_open = os.open
    switched = False

    def racing_open(name, flags, *args, **kwargs):
        nonlocal switched
        if os.fspath(name) == "ALGO.md" and not switched:
            switched = True
            path.unlink()
            path.symlink_to(secret)
        return original_open(name, flags, *args, **kwargs)

    if os.name == "nt":
        pytest.skip("POSIX descriptor-relative open race; Windows reader has separate native coverage")
    supported = config._directory_descriptor_io_supported()
    monkeypatch.setattr(config, "_directory_descriptor_io_supported", lambda: supported)
    monkeypatch.setattr(os, "open", racing_open)
    result = catalog.algorithm_catalog_read()
    assert switched and result.startswith("Error:") and "PRIVATE_CANARY" not in result


def test_postwrite_verification_failure_does_not_claim_success(monkeypatch):
    path = _owner_catalog()
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    original_write = config._atomic_write_text

    def changed_after_write(target, text):
        original_write(target, text)
        target.write_text("# concurrent external edit\n", encoding="utf-8")

    monkeypatch.setattr(config, "_atomic_write_text", changed_after_write)
    assert "post-write verification failed" in catalog.algorithm_catalog_edit(digest, "proposed", "planned")
    assert path.read_text() == "# concurrent external edit\n"


def test_concurrent_edits_with_same_digest_do_not_lose_updates():
    path = _owner_catalog()
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: catalog.algorithm_catalog_edit(digest, "proposed", value), ["planned", "retired"]))
    assert sum(result.startswith("Error:") for result in results) == 1
    assert sum('"post_write_verified": true' in result for result in results) == 1
    assert path.read_text().count("Status:") == 1


@pytest.mark.parametrize("mode", ["execute", "yolo"])
def test_runtime_dispatch_admits_only_typed_catalog_and_retains_protected_roots(tmp_path, mode):
    path = _owner_catalog()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = Config(cwd=str(workspace), continuum_enabled=True, session_mode=mode)
    cfg._algo_program_authorization = authorization_for_actions(tuple(tools.TOOL_MAP))
    if mode == "yolo":
        restore_yolo_activation(cfg)
    cfg._nathan_approval_mode = "never"
    result = dispatch_action("algorithm_catalog_read", {}, cfg, render=False)
    assert result.outcome.invoked and result.status == "worked"
    assert json.loads(result.result)["content"] == path.read_text()
    assert json.loads(tools.action_search("algorithm_catalog_read", limit=1, cfg=cfg))["count"] == 1
    for sibling in ("ALGO.md", "config.json", "env", "identity/SOUL.md"):
        raw = dispatch_action("read_file", {"path": str(config.CONFIG_DIR / sibling)}, cfg, render=False)
        assert not raw.outcome.invoked
    before = path.read_bytes()
    args = {"expected_sha256": json.loads(result.result)["sha256"], "old_text": "proposed", "new_text": "planned"}
    edit = dispatch_action("algorithm_catalog_edit", args, cfg, render=False)
    if mode == "yolo":
        assert edit.outcome.invoked and json.loads(edit.result)["post_write_verified"]
    else:
        assert not edit.outcome.invoked and path.read_bytes() == before


def test_auto_is_not_action_time_edit_permission(tmp_path):
    _owner_catalog()
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True, auto_mode=True)
    cfg._nathan_approval_mode = "auto"
    digest = json.loads(catalog.algorithm_catalog_read())["sha256"]
    args = {"expected_sha256": digest, "old_text": "proposed", "new_text": "planned"}
    assert not nathan_runtime.ask_approval("algorithm_catalog_edit", args, cfg)
    assert policy_for_action("algorithm_catalog_edit").confirmation_mode is ConfirmationMode.ACTION_TIME
    assert policy_for_action("algorithm_catalog_edit").target_scope is TargetScope.RUNTIME


def test_tool_has_no_path_escape_and_guidance_overrides_generic_navigation():
    import inspect

    assert "path" not in inspect.signature(tools.TOOL_MAP["algorithm_catalog_read"]).parameters
    assert "path" not in inspect.signature(tools.TOOL_MAP["algorithm_catalog_edit"]).parameters
    guidance = prompt_rules.catalog_guidance()
    assert "algorithm_catalog_read" in guidance and "algorithm_catalog_edit" in guidance
    assert "not filesystem tools" in guidance
    assert "Never publish" in guidance


def _protected_reference_tools(monkeypatch):
    monkeypatch.setattr(harness, "_PROTECTED_MEMORY_AUTHORITY", True)
    index = {"records": [], "record_count": 0}
    monkeypatch.setattr(harness, "load_index", lambda **kwargs: index)
    monkeypatch.setattr(harness, "_runtime_capability_records", lambda **kwargs: [])
    return Config(continuum_enabled=True), index


@pytest.mark.parametrize("query,expected", [
    ("decision intervention budget", "P5"),
    ("context reduction specialization", "H20"),
])
def test_protected_explicit_search_projects_catalog_without_persisting(monkeypatch, query, expected):
    path = _owner_catalog(
        "# Synthetic catalog\n"
        "### P5. Decision Intervention Budget\nStatus: proposed\nSynthetic budget fixture.\n"
        "### H20. Context Reduction Specialization\nStatus: proposed\nSynthetic reduction fixture.\n"
    )
    cfg, persisted = _protected_reference_tools(monkeypatch)
    (config.CONFIG_DIR / "env").write_text("SIBLING_PRIVATE_CANARY")
    output = tools.harness_search(query, kind="algorithm", limit=1, cfg=cfg)
    assert f"ALGO.md#{expected}" in output
    assert "owner_algorithm_catalog" in output
    assert "SIBLING_PRIVATE_CANARY" not in output
    assert persisted == {"records": [], "record_count": 0}
    assert "SIBLING_PRIVATE_CANARY" not in path.read_text()
    assert not harness._protected_memory_source_allowed(
        harness.SourceRoot("algo-cli", "algorithm", config.CONFIG_DIR, ("*",), 1)
    )


def test_owner_projection_is_explicit_not_automatic_context(monkeypatch):
    _owner_catalog("### P5. Private Reference Fixture\nStatus: proposed\nCATALOG_CANARY\n")
    cfg, _ = _protected_reference_tools(monkeypatch)
    assert "CATALOG_CANARY" not in str(harness.retrieval_index(protected_memory=True))
    assert "ALGO.md#P5" not in tools.harness_search("fixture", kind="memory", cfg=cfg)
    assert "ALGO.md#P5" not in tools.harness_search("fixture", harness_name="codex", cfg=cfg)
    assert "ALGO.md#P5" in tools.harness_search("fixture", cfg=cfg)


def test_catalog_search_revalidates_changed_and_removed_source(monkeypatch):
    path = _owner_catalog("### P5. Original Fixture\nStatus: proposed\n")
    cfg, _ = _protected_reference_tools(monkeypatch)
    assert "ALGO.md#P5" in tools.harness_search("original fixture", cfg=cfg)
    path.write_text("### H20. Replacement Fixture\nStatus: proposed\n")
    output = tools.harness_search("replacement fixture", kind="algorithm", cfg=cfg)
    assert "ALGO.md#H20" in output and "ALGO.md#P5" not in output
    path.unlink()
    assert "ALGO.md#H20" not in tools.harness_search("replacement fixture", cfg=cfg)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "invalid-utf8", "duplicate-id"])
def test_unsafe_owner_catalog_search_fails_closed(monkeypatch, unsafe):
    cfg, _ = _protected_reference_tools(monkeypatch)
    secret = config.CONFIG_DIR / "env"
    secret.write_text("SIBLING_PRIVATE_CANARY")
    if unsafe == "invalid-utf8":
        _owner_catalog().write_bytes(b"\xff")
    elif unsafe == "duplicate-id":
        _owner_catalog("### A1. One\nStatus: proposed\n### A1. Two\nStatus: proposed\n")
    elif unsafe == "hardlink":
        os.link(secret, config.CONFIG_DIR / "ALGO.md")
    else:
        try:
            (config.CONFIG_DIR / "ALGO.md").symlink_to(secret)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    output = tools.harness_search("fixture", kind="algorithm", cfg=cfg)
    assert output.startswith("Error:")
    assert "SIBLING_PRIVATE_CANARY" not in output


def test_exact_pattern_read_ignores_forged_index_path_and_reads_fresh_source(monkeypatch):
    path = _owner_catalog("### P5. Original Fixture\nStatus: proposed\nCURRENT_REFERENCE\n")
    cfg, index = _protected_reference_tools(monkeypatch)
    secret = config.CONFIG_DIR / "env"
    secret.write_text("SIBLING_PRIVATE_CANARY")
    index["records"].append({"id": "algo-cli:algorithm:ALGO.md#P5", "path": str(secret), "kind": "algorithm"})
    monkeypatch.setattr(harness, "get_record", lambda _: index["records"][0])
    output = tools.harness_read("algo-cli:algorithm:ALGO.md#P5", cfg=cfg)
    assert "CURRENT_REFERENCE" in output and "SIBLING_PRIVATE_CANARY" not in output
    assert "reference" in output.lower() and "SHA256:" in output
    path.write_text("### P5. Changed Fixture\nStatus: proposed\nUPDATED_REFERENCE\n")
    assert "UPDATED_REFERENCE" in tools.harness_read("algo-cli:algorithm:ALGO.md#P5", cfg=cfg)
    for suffix in ("../env", "P5/../../env", "", "P5?path=env"):
        assert tools.harness_read("algo-cli:algorithm:ALGO.md#" + suffix, cfg=cfg).startswith("Error:")


def test_refresh_reports_public_index_and_query_time_owner_patterns(monkeypatch):
    _owner_catalog()
    cfg, persisted = _protected_reference_tools(monkeypatch)
    output = tools.harness_refresh(cfg=cfg)
    assert "Owner catalog patterns: 1" in output
    assert "not persisted" in output
    assert "No embedding calls" in output
    assert persisted == {"records": [], "record_count": 0}


def test_owner_reference_routes_do_not_log_private_bodies():
    for name in ("harness_search", "harness_read"):
        policy = policy_for_action(name)
        assert policy.suppress_logs is True
        assert any(item.value == "sensitive" for item in policy.data_classes)
