"""Capability discovery must cover the real registry without granting authority."""

import json

import pytest

from algo_cli import action_registry, harness, tools
from algo_cli.oliver_slash_dispatch import SLASH_COMMANDS


def test_capability_index_covers_every_runtime_tool_and_slash():
    records = harness._runtime_capability_records()
    indexed = {(row["capability"]["kind"], row["capability"]["name"]) for row in records}
    assert {("tool", name) for name in tools.TOOL_MAP} <= indexed
    assert {("slash", name) for name, _ in SLASH_COMMANDS} <= indexed
    assert len({row["id"] for row in records}) == len(records)


@pytest.mark.parametrize("name", ["edit_file", "git_status", "git_diff", "web_fetch"])
def test_previously_omitted_tools_are_searchable_readable_and_policy_accurate(monkeypatch, name):
    records = harness._runtime_capability_records()
    monkeypatch.setattr(harness, "load_index", lambda **_kwargs: {"records": records})
    expected = f"algo-cli:runtime_capability:{name}"
    hits = harness.search_index(name, kind="runtime_capability", limit=5)
    assert expected in {row["id"] for row in hits}
    record = next(row for row in records if row["id"] == expected)
    assert record["capability"] == action_registry.get_action_spec(name).as_dict()
    assert f"Capability {name}." in harness.read_record(expected)


def test_unknown_callable_is_described_without_execution_or_new_authority(monkeypatch):
    def unknown_probe():
        pytest.fail("catalog construction must not execute a tool")

    monkeypatch.setattr(tools, "TOOL_MAP", {"unknown_probe": unknown_probe})
    before = action_registry.policy_for_action("unknown_probe")
    records = harness._runtime_capability_records()
    record = next(row for row in records if row["id"] == "algo-cli:runtime_capability:unknown_probe")
    assert record["capability"]["curated"] is False
    assert record["capability"]["requires_approval"] is True
    assert "denied until an explicit authority policy" in record["index_text"]
    assert action_registry.policy_for_action("unknown_probe") == before


def test_existing_index_upgrades_explicit_only_catalog_and_reuses_unchanged_vectors(monkeypatch, tmp_path):
    source = tmp_path / "public"
    source.mkdir()
    (source / "README.md").write_text("# Public fixture\n\nRuntime documentation.\n")
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("algo-cli", "wiki", source, ("*.md",), 1),))
    index = harness.build_index()
    explicit = {spec.name for spec in action_registry.list_action_specs(include_archived=True)}
    index["records"] = [
        row for row in index["records"] if row["kind"] != "runtime_capability" or row["capability"]["name"] in explicit
    ]
    record = next(row for row in index["records"] if row["id"] == "algo-cli:runtime_capability:write_file")
    record["embedding"], record["embedding_model"] = [1.0, 0.5], "fixture"
    index["record_count"] = len(index["records"])
    index["source_policy"].pop("effective_runtime_capabilities", None)
    harness.INDEX_PATH.write_text(json.dumps(index))
    harness._set_index_cache(None)
    assert harness.index_is_stale()
    updated = harness.load_index()
    assert "algo-cli:runtime_capability:edit_file" in {row["id"] for row in updated["records"]}
    retained = next(row for row in updated["records"] if row["id"] == record["id"])
    assert retained["embedding"] == [1.0, 0.5]
    assert retained["embedding_model"] == "fixture"


def test_fully_embedded_explicit_only_catalog_is_not_ready():
    explicit = {spec.name for spec in action_registry.list_action_specs(include_archived=True)}
    records = [row for row in harness._runtime_capability_records() if row["capability"]["name"] in explicit]
    quality = harness._index_quality_summary(records, {"complete": True})
    assert quality["status"] == "degraded"
    coverage = quality["runtime_capability_coverage"]
    assert coverage["complete"] is False
    assert "algo-cli:runtime_capability:edit_file" in coverage["missing_ids"]
    assert coverage["indexed"] == len(records)
    assert coverage["expected"] > coverage["indexed"]
    assert any("/harness refresh" in message for message in quality["recommendations"])


@pytest.mark.parametrize("persisted", [False, True])
def test_effective_catalog_has_complete_coverage(persisted):
    records = harness._runtime_capability_records()
    if persisted:
        records = json.loads(json.dumps(records))
    quality = harness._index_quality_summary(records, {"complete": True})
    assert quality["status"] == "ready"
    coverage = quality["runtime_capability_coverage"]
    assert coverage["complete"] is True
    assert coverage["expected"] == coverage["indexed"] == len(records)


@pytest.mark.parametrize(
    "defect",
    [
        "stale_policy",
        "numeric_policy",
        "nonfinite",
        "unserializable",
        "duplicate",
        "unknown",
        "malformed",
    ],
)
def test_capability_count_cannot_hide_invalid_inventory(defect):
    records = harness._runtime_capability_records()
    if defect == "stale_policy":
        records[0]["capability"]["requires_approval"] = not records[0]["capability"]["requires_approval"]
    elif defect == "numeric_policy":
        records[0]["capability"]["requires_approval"] = int(records[0]["capability"]["requires_approval"])
    elif defect == "nonfinite":
        records[0]["capability"]["invalid"] = float("nan")
    elif defect == "unserializable":
        records[0]["capability"]["invalid"] = {"not", "json"}
    elif defect == "duplicate":
        records.append(dict(records[0]))
    elif defect == "unknown":
        records.append({**records[0], "id": "algo-cli:runtime_capability:nonexistent"})
    else:
        records[0]["capability"] = None
    quality = harness._index_quality_summary(records, {"complete": True})
    assert quality["status"] == "degraded"
    assert quality["runtime_capability_coverage"]["complete"] is False
