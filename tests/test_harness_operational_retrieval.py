"""Protected operational retrieval exposes shipped contracts, never host memory."""

from copy import deepcopy
from pathlib import Path
import os
import shutil
from types import SimpleNamespace

import pytest

from algo_cli import config, harness, main, tool_context, tools
from algo_cli.config import Config


@pytest.fixture
def public_contracts(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = Path(__file__).resolve().parents[1] / "docs"
    root = harness.SourceRoot("algo-cli", "memory", docs, harness.CURATED_PROJECT_MEMORY_DOCS, 3)
    records = []
    for name in harness.CURATED_PROJECT_MEMORY_DOCS:
        shutil.copyfile(source / name, docs / name)
        row = harness.make_record(root, docs / name)
        row.update(embedding=[1.0, 0.0], embedding_model="fixture", embedding_dimensions=None)
        records.append(row)
    private = tmp_path / "operator-memory.md"
    private.write_text("PRIVATE_HOST_MEMORY_CANARY")
    records.append(
        {
            "id": "legacy-memory",
            "kind": "memory",
            "harness": "algo-cli",
            "path": str(private),
            "title": "Private host memory",
            "summary": "PRIVATE_HOST_MEMORY_CANARY",
            "search_text": "memory",
            "embedding": [1.0, 0.0],
            "embedding_model": "fixture",
            "embedding_dimensions": None,
        }
    )
    index = {"records": records, "record_count": len(records)}
    monkeypatch.setattr(harness, "_algo_cli_docs_dir", lambda: docs)
    monkeypatch.setattr(harness, "_PROTECTED_MEMORY_AUTHORITY", True)
    monkeypatch.setattr(harness, "load_index", lambda **_: index)
    monkeypatch.setattr(harness, "get_record", lambda rid: next((row for row in records if row["id"] == rid), None))
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    monkeypatch.setattr(harness, "resolve_embed_model", lambda _: "fixture")
    monkeypatch.setattr(main, "make_local_embed_fn", lambda *_args, **_kwargs: lambda _: [[1.0, 0.0]])
    return docs, records, cfg


def test_protected_tool_search_and_read_expose_source_verified_contract(public_contracts):
    _, records, cfg = public_contracts
    rendered = tools.harness_search("memory placement retention", kind="memory", limit=3, cfg=cfg)
    assert records[0]["id"] in rendered
    assert "shipped_product_documentation" in rendered
    assert "hybrid" in rendered
    assert "PRIVATE_HOST_MEMORY_CANARY" not in rendered
    body = tools.harness_read(records[0]["id"], cfg=cfg)
    assert "User instructions and verified live files" in body
    assert "Shipped documentation; not agent memory" in body
    assert "only through Echo Veil" in tools.harness_read("legacy-memory", cfg=cfg)


@pytest.mark.parametrize("kind,harness_name", [(" MeMoRy ", " ALGO-CLI "), ("memory", "all"), ("memory", None)])
def test_search_filters_are_normalized_and_disclosed(public_contracts, kind, harness_name):
    _, records, cfg = public_contracts
    output = tools.harness_search("placement retention", kind=kind, harness_name=harness_name, cfg=cfg)
    assert records[0]["id"] in output
    assert "Active filters:" in output and "kind='memory'" in output
    assert "  kind: memory\n" in output
    assert "PRIVATE_HOST_MEMORY_CANARY" not in output


@pytest.mark.parametrize("filters", [{"kind": "wiki"}, {"kind": "policy"}, {"harness_name": "missing"}])
def test_filtered_empty_search_reports_authorized_facets_without_widening(public_contracts, monkeypatch, filters):
    _, records, cfg = public_contracts
    records[-1].update(kind="private-kind", harness="private-harness")
    monkeypatch.setattr(main, "make_local_embed_fn", lambda *args, **kwargs: lambda _: pytest.fail("empty slice"))
    output = tools.harness_search("placement retention", cfg=cfg, **filters)
    assert output.startswith("No harness matches.")
    assert "Active filters:" in output
    assert "Indexed harnesses: algo-cli" in output
    assert "Indexed kinds: memory" in output
    assert "shipped policy contracts" in output and "not agent memory" in output
    assert "private-kind" not in output and "private-harness" not in output
    assert not any(record["id"] in output for record in records)


def test_search_schema_explains_primary_harness_and_contract_kind():
    from algo_cli import chatgpt_client

    schema = chatgpt_client._build_responses_tools([tools.harness_search])[0]
    fields = schema["parameters"]["properties"]
    assert "algo-cli" in fields["harness_name"]["description"]
    assert "shipped policy contracts" in fields["kind"]["description"]
    assert "Leave unset" in fields["kind"]["description"]


@pytest.fixture
def alias_records(monkeypatch):
    records = [
        {
            "id": f"{name}:{kind}:guide",
            "harness": name,
            "kind": kind,
            "title": "Guide",
            "path": f"/public/{name}/{kind}.md",
            "search_text": "guide",
            "embedding": [1.0, 0.0],
            "embedding_model": "fixture",
        }
        for name in ("algo-cli", "codex", "claude", "openclaw")
        for kind in ("wiki", "skill")
    ]
    monkeypatch.setattr(harness, "retrieval_index", lambda **_: {"records": records})
    monkeypatch.setattr(main, "make_local_embed_fn", lambda *_args, **_kwargs: lambda _: [[1.0, 0.0]])
    return records


@pytest.mark.parametrize("use_hybrid", [False, True])
@pytest.mark.parametrize("kind", [None, "wiki"])
@pytest.mark.parametrize(
    "alias,names",
    [
        ("openclaude", {"claude", "openclaw"}),
        (" CLAUDE-CODE ", {"claude"}),
        ("codex-cli", {"codex"}),
        ("codex", {"codex"}),
        ("all", {"algo-cli", "codex", "claude", "openclaw"}),
        ("missing", set()),
    ],
)
def test_tool_filter_aliases_match_ranker_scope(alias_records, alias, names, kind, use_hybrid):
    cfg = (
        SimpleNamespace(echo_veil_enabled=False, echo_veil_protection="optional", harness_embed_model="fixture")
        if use_hybrid
        else None
    )
    output = tools.harness_search("guide", harness_name=alias, kind=kind, cfg=cfg)
    returned = {line.removeprefix("- ") for line in output.splitlines() if line.startswith("- ")}
    expected = {row["id"] for row in alias_records if row["harness"] in names and (kind is None or row["kind"] == kind)}
    assert returned == expected
    if alias.strip().casefold() != "all":
        assert f"harness={alias.strip().casefold()!r}" in output


def test_alias_cannot_widen_protected_snapshot_or_bypass_kind_filter(alias_records, monkeypatch):
    authorized = [row for row in alias_records if row["harness"] in {"algo-cli", "claude"}]
    projections = []

    def project(*, protected_memory):
        projections.append(protected_memory)
        return {"records": authorized}

    monkeypatch.setattr(harness, "retrieval_index", project)
    # Even a ranker returning outside or unfiltered rows cannot expand authority.
    monkeypatch.setattr(harness, "hybrid_search", lambda *_args, **_kwargs: alias_records)
    cfg = SimpleNamespace(echo_veil_enabled=True, echo_veil_protection="required", harness_embed_model="fixture")
    output = tools.harness_search("guide", harness_name="openclaude", kind="wiki", cfg=cfg)
    returned = [line.removeprefix("- ") for line in output.splitlines() if line.startswith("- ")]
    assert returned == ["claude:wiki:guide"]
    assert projections == [True]


def test_projection_rebuilds_untrusted_metadata_and_keeps_unchanged_vectors(public_contracts):
    _, records, _ = public_contracts
    records[0]["summary"] = "PRIVATE_HOST_MEMORY_CANARY"
    records[0]["title"] = "PRIVATE_HOST_MEMORY_CANARY"
    before = deepcopy(records)
    projected = harness.retrieval_index(protected_memory=True)
    assert len(projected["records"]) == 3
    assert all(row["source_kind"] == "shipped_product_documentation" for row in projected["records"])
    assert "PRIVATE_HOST_MEMORY_CANARY" not in str(projected)
    assert projected["records"][0]["embedding"] == [1.0, 0.0]
    assert records == before


def test_projection_drops_stale_vectors_when_source_text_changes(public_contracts):
    docs, records, _ = public_contracts
    path = docs / harness.CURATED_PROJECT_MEMORY_DOCS[0]
    path.write_text("# Changed public contract\n\nNew implementation guidance.\n")
    selected = harness.retrieval_index(protected_memory=True)["records"][0]
    assert selected["id"] == records[0]["id"]
    assert "embedding" not in selected
    assert "New implementation guidance" in selected["index_text"]


def test_revalidated_projection_reuses_records_without_skipping_source_reads(public_contracts, monkeypatch):
    reads = []
    original = harness._read_public_source

    def checked(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(harness, "_read_public_source", checked)
    first = harness.retrieval_index(protected_memory=True)
    second = harness.retrieval_index(protected_memory=True)
    assert second["records"] is first["records"]
    assert len(reads) == 2 * len(harness.CURATED_PROJECT_MEMORY_DOCS)


def test_warmed_projection_rejects_later_source_replacement(public_contracts, tmp_path):
    docs, records, _ = public_contracts
    first = harness.retrieval_index(protected_memory=True)
    path = docs / harness.CURATED_PROJECT_MEMORY_DOCS[0]
    target = tmp_path / "private.txt"
    target.write_text("PRIVATE_HOST_MEMORY_CANARY")
    path.unlink()
    path.symlink_to(target)
    second = harness.retrieval_index(protected_memory=True)
    assert second["records"] is not first["records"]
    assert records[0]["id"] not in {row["id"] for row in second["records"]}
    assert "PRIVATE_HOST_MEMORY_CANARY" not in str(second)


def test_warmed_projection_isolates_and_invalidates_changed_vectors(public_contracts):
    _, records, _ = public_contracts
    capability = harness._runtime_capability_records()[0]
    capability.update(embedding=[1.0, 0.0], embedding_model="fixture")
    records.append(capability)
    first = harness.retrieval_index(protected_memory=True)
    capability["embedding"][0] = 0.5
    records[0]["embedding"][0] = 0.25
    second = harness.retrieval_index(protected_memory=True)
    assert second["records"] is not first["records"]
    assert first["records"][-1]["embedding"][0] == 1.0
    assert first["records"][0]["embedding"][0] == 1.0
    assert second["records"][-1]["embedding"][0] == 0.5
    assert second["records"][0]["embedding"][0] == 0.25


def test_mutated_projection_metadata_is_not_reused(public_contracts):
    first = harness.retrieval_index(protected_memory=True)
    first["records"][0]["summary"] = "PRIVATE_HOST_MEMORY_CANARY"
    second = harness.retrieval_index(protected_memory=True)
    assert "PRIVATE_HOST_MEMORY_CANARY" not in str(second)


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "/tmp/operator-memory.md"),
        ("relative_path", "../operator-memory.md"),
        ("harness", "codex"),
        ("id", "forged"),
    ],
)
def test_record_metadata_cannot_grant_contract_access(public_contracts, field, value):
    _, records, cfg = public_contracts
    records[0][field] = value
    records[0]["source_kind"] = "shipped_product_documentation"
    assert harness.checked_product_contract(records[0]) is None
    assert "only through Echo Veil" in tools.harness_read(records[0]["id"], cfg=cfg)


@pytest.mark.parametrize("link", ["symlink", "hardlink", "directory_symlink"])
def test_contract_links_fail_closed(public_contracts, tmp_path, link):
    docs, records, cfg = public_contracts
    path = docs / harness.CURATED_PROJECT_MEMORY_DOCS[0]
    target = tmp_path / "private.txt"
    target.write_text("PRIVATE_HOST_MEMORY_CANARY")
    if link == "directory_symlink":
        moved = tmp_path / "moved"
        docs.rename(moved)
        docs.symlink_to(moved, target_is_directory=True)
    else:
        path.unlink()
        if link == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    assert harness.checked_product_contract(records[0]) is None
    assert "PRIVATE_HOST_MEMORY_CANARY" not in tools.harness_read(records[0]["id"], cfg=cfg)


def test_contract_replacement_during_descriptor_read_fails_closed(public_contracts, monkeypatch):
    docs, records, _ = public_contracts
    path = docs / harness.CURATED_PROJECT_MEMORY_DOCS[0]
    original = config.os.read
    changed = False

    def racing_read(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            path.unlink()
            path.write_text("PRIVATE_HOST_MEMORY_CANARY")
        return data

    monkeypatch.setattr(config.os, "read", racing_read)
    assert harness.checked_product_contract(records[0]) is None
    assert changed


def test_hybrid_uses_one_index_snapshot(monkeypatch):
    calls = []
    record = {
        "id": "public",
        "harness": "algo-cli",
        "kind": "wiki",
        "search_text": "query",
        "embedding": [1.0, 0.0],
        "embedding_model": "fixture",
    }

    def load():
        calls.append(True)
        if len(calls) > 1:
            pytest.fail("rankers must not reopen a different snapshot")
        return {"records": [record]}

    monkeypatch.setattr(harness, "load_index", load)
    assert harness.hybrid_search("query", lambda _: [[1.0, 0.0]], "fixture")[0]["id"] == "public"
    assert len(calls) == 1


def test_tool_discloses_keyword_fallback_without_bulk_embedding(public_contracts, monkeypatch):
    _, records, cfg = public_contracts
    monkeypatch.setattr(main, "make_local_embed_fn", lambda *_args, **_kwargs: lambda _: [])
    monkeypatch.setattr(
        harness, "embed_index_records", lambda *_args, **_kwargs: pytest.fail("search must not embed the corpus")
    )
    result = tools.harness_search("memory placement retention", kind="memory", cfg=cfg)
    assert records[0]["id"] in result
    assert "keyword-only" in result
    assert "PRIVATE_HOST_MEMORY_CANARY" not in result


def test_tool_cancellation_propagates(public_contracts, monkeypatch):
    _, _, cfg = public_contracts

    def cancel(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "make_local_embed_fn", lambda *_args, **_kwargs: cancel)
    with pytest.raises(KeyboardInterrupt):
        tools.harness_search("memory", kind="memory", cfg=cfg)


def test_unprepared_protected_view_does_not_open_index(public_contracts, monkeypatch):
    _, records, cfg = public_contracts
    monkeypatch.setattr(harness, "_PROTECTED_MEMORY_AUTHORITY", False)
    monkeypatch.setattr(harness, "load_index", lambda: pytest.fail("do not read unprepared legacy index"))
    assert "unavailable" in tools.harness_search("memory", cfg=cfg)
    assert "unavailable" in tools.harness_read(records[0]["id"], cfg=cfg)
    assert "unavailable" in harness.read_record(records[0]["id"], protected_memory=True)


@pytest.mark.parametrize("malformation", ["oversized", "invalid_utf8", "directory"])
def test_contract_payload_bounds(public_contracts, malformation):
    docs, records, _ = public_contracts
    path = docs / harness.CURATED_PROJECT_MEMORY_DOCS[0]
    if malformation == "oversized":
        path.write_bytes(b"x" * (256 * 1024 + 1))
    elif malformation == "invalid_utf8":
        path.write_bytes(b"\xff\xfe\xff")
    else:
        path.unlink()
        path.mkdir()
    assert harness.checked_product_contract(records[0]) is None


def test_forged_capability_cannot_surface_mutable_text(public_contracts):
    _, records, cfg = public_contracts
    valid = harness._runtime_capability_records()[0]
    valid.update(
        title="PRIVATE_HOST_MEMORY_CANARY",
        summary="PRIVATE_HOST_MEMORY_CANARY",
        index_text="PRIVATE_HOST_MEMORY_CANARY",
    )
    records.extend([valid, {**valid, "id": "unregistered-capability"}])
    view = harness.retrieval_index(protected_memory=True)
    assert "PRIVATE_HOST_MEMORY_CANARY" not in str(view)
    assert all(row["id"] != "unregistered-capability" for row in view["records"])
    assert "PRIVATE_HOST_MEMORY_CANARY" not in tools.harness_read(valid["id"], cfg=cfg)


def test_hybrid_explicit_snapshot_does_not_reload(monkeypatch):
    monkeypatch.setattr(harness, "load_index", lambda: pytest.fail("explicit snapshot must be used"))
    snapshot = {"records": [{"id": "public", "kind": "wiki", "harness": "algo-cli", "search_text": "query"}]}
    assert harness.hybrid_search("query", lambda _: [], index=snapshot)[0]["id"] == "public"
    assert harness.search_index("query", index=snapshot)[0]["id"] == "public"


def test_ranker_payload_cannot_override_authorized_metadata(public_contracts, monkeypatch):
    _, records, cfg = public_contracts
    monkeypatch.setattr(
        harness,
        "hybrid_search",
        lambda *_args, **_kwargs: [
            {"id": records[0]["id"], "title": "PRIVATE_HOST_MEMORY_CANARY"},
            {"id": "legacy-memory", "title": "PRIVATE_HOST_MEMORY_CANARY"},
        ],
    )
    result = tools.harness_search("memory", cfg=cfg)
    assert records[0]["id"] in result
    assert "PRIVATE_HOST_MEMORY_CANARY" not in result
    assert "legacy-memory" not in result


@pytest.mark.parametrize("source_kind", ["wiki", "algorithm"])
def test_protected_public_read_rejects_link_replacement(public_contracts, tmp_path, source_kind, monkeypatch):
    docs, records, cfg = public_contracts
    monkeypatch.setattr(harness, "_algo_cli_repo_dir", lambda: tmp_path)
    if source_kind == "algorithm":
        path = docs / "ALGO.md"
        path.write_text("### O1. Public pattern\n**Status:** proposed\nPublic source body.\n")
        root = harness.SourceRoot("algo-cli", "algorithm", docs, ("ALGO.md",), 1)
        index = harness._merge_pattern_records({"records": [harness.make_record(root, path)]})
        record = next(row for row in index["records"] if row.get("pattern_id"))
    else:
        path = docs / "provider-auth-recovery.md"
        path.write_text("# Public runbook\n\nPublic source body.\n")
        root = harness.SourceRoot("algo-cli", "wiki", docs, (path.name,), 1)
        record = harness.make_record(root, path)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (root,))
    records.append(record)
    assert "Public source body." in tools.harness_read(record["id"], cfg=cfg)
    target = tmp_path / "private.txt"
    target.write_text("PRIVATE_HOST_MEMORY_CANARY")
    path.unlink()
    path.symlink_to(target)
    result = tools.harness_read(record["id"], cfg=cfg)
    assert result.startswith("Error:")
    assert "PRIVATE_HOST_MEMORY_CANARY" not in result


def test_query_embedding_timeout_reaches_both_local_transports(monkeypatch):
    captured = {}

    def gateway(*_args, **kwargs):
        captured["gateway_timeout"] = kwargs["timeout_seconds"]
        return None

    class Client:
        def __init__(self, **kwargs):
            captured["client_timeout"] = kwargs["timeout"]

        def embed(self, **kwargs):
            captured["model"] = kwargs["model"]
            return {"embeddings": [[1.0, 0.0]]}

    monkeypatch.setattr(tools, "gateway_ready", lambda: True)
    monkeypatch.setattr(tools, "gateway_embed_batch", gateway)
    monkeypatch.setattr(main, "Client", Client)
    assert main.make_local_embed_fn(Config(), "fixture", timeout_seconds=10.0)(["query"]) == [[1.0, 0.0]]
    assert captured == {"gateway_timeout": 10.0, "client_timeout": 10.0, "model": "fixture"}


@pytest.mark.parametrize("name", ["harness_search", "harness_read", "echo_veil_doctor"])
@pytest.mark.parametrize("form", ["Use {name}.", "Use `{name}`.", "Use {name}() now."])
def test_explicit_tool_identifier_satisfies_specialist_intent(name, form):
    selected = tool_context.select_tools_for_prompt(form.format(name=name.upper()), tools.ALL_TOOLS)
    assert name in {tool.__name__ for tool in selected}
    assert len(selected) <= tool_context.DEFAULT_TOOL_LIMIT


@pytest.mark.parametrize("query", ["not_harness_search", "harness_search_extra", "harness_search2"])
def test_partial_tool_identifier_does_not_satisfy_specialist_intent(query):
    selected = tool_context.select_tools_for_prompt(query, [tools.harness_search])
    assert selected == []


@pytest.mark.parametrize("classes", ["browser", "filesystem"])
def test_explicit_name_cannot_override_declared_tool_classes(classes):
    selected = tool_context.select_tools_for_prompt(
        f"Allowed tool classes: {classes}\nUse harness_search.", tools.ALL_TOOLS
    )
    assert "harness_search" not in {tool.__name__ for tool in selected}


def test_identifier_discovery_never_invokes_a_tool():
    def harness_search():
        pytest.fail("discovery must not execute a tool")

    assert tool_context.select_tools_for_prompt("Use harness_search", [harness_search]) == [harness_search]
