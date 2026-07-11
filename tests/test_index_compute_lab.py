"""index-compute-lab bridge (offline)."""

from __future__ import annotations

from algo_cli import index_compute_lab


def test_resolve_lab_root_env(monkeypatch, tmp_path):
    lab = tmp_path / "lab"
    lab.mkdir()
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(lab))
    assert index_compute_lab.resolve_lab_root() == lab.resolve()


def test_lab_available_requires_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    assert index_compute_lab.lab_available() is False
    (tmp_path / "query.py").write_text("# stub\n", encoding="utf-8")
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (atoms / "ranked-association-map.json").write_text('{"schema_version":"1.0.0","index":{}}', encoding="utf-8")
    (atoms / "alias-table.json").write_text('{"schema_version":"1.0.0","aliases":[]}', encoding="utf-8")
    assert index_compute_lab.lab_available() is True


def test_context_for_query_uses_run_ask(monkeypatch, tmp_path):
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (tmp_path / "query.py").write_text("print('ok')", encoding="utf-8")
    (atoms / "ranked-association-map.json").write_text("{}", encoding="utf-8")
    (atoms / "alias-table.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(index_compute_lab, "run_ask", lambda q, **kw: "graph-hit")
    assert index_compute_lab.context_for_query("sample project permit") == "graph-hit"


def test_harness_all_source_roots_includes_lab(tmp_path, monkeypatch):
    from algo_cli import harness

    monkeypatch.setattr(harness, "_INDEX_COMPUTE_LAB_SOURCE_ENABLED", True)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (atoms / "note.md").write_text("# note\n", encoding="utf-8")
    roots = harness.all_source_roots()
    names = {r.harness for r in roots}
    assert "index-compute-lab" in names


def test_harness_all_source_roots_dedupes_icl_extra_root(tmp_path, monkeypatch):
    from algo_cli import harness

    monkeypatch.setattr(harness, "_INDEX_COMPUTE_LAB_SOURCE_ENABLED", True)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (atoms / "note.md").write_text("# note\n", encoding="utf-8")
    monkeypatch.setattr(
        harness,
        "load_extra_source_roots",
        lambda: [
            harness.SourceRoot(
                "index-compute-lab",
                "memory",
                atoms,
                ("*.md",),
                120,
            )
        ],
    )

    roots = [
        root
        for root in harness.all_source_roots()
        if root.harness == "index-compute-lab" and root.kind == "memory"
    ]
    assert len(roots) == 1
    assert roots[0].root.resolve() == atoms.resolve()


def test_harness_does_not_auto_enroll_lab(tmp_path, monkeypatch):
    from algo_cli import harness

    monkeypatch.setattr(harness, "_INDEX_COMPUTE_LAB_SOURCE_ENABLED", False)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (atoms / "note.md").write_text("# note\n", encoding="utf-8")

    assert "index-compute-lab" not in {root.harness for root in harness.all_source_roots()}


def test_ensure_harness_roots_file_removes_legacy_icl_entry(tmp_path, monkeypatch):
    from algo_cli import config as config_mod

    roots_path = tmp_path / "harness_roots.json"
    roots_path.write_text(
        '[{"harness":"index-compute-lab","kind":"memory","root":"/tmp/lab/atoms","patterns":["*.md"],"max_files":120},'
        '{"harness":"custom","kind":"wiki","root":"/tmp/wiki","patterns":["*.md"],"max_files":20}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(index_compute_lab, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

    assert index_compute_lab.ensure_harness_roots_file() is True
    remaining = roots_path.read_text(encoding="utf-8")
    assert "index-compute-lab" not in remaining
    assert "custom" in remaining
    assert index_compute_lab.ensure_harness_roots_file() is False


def test_write_graph_note(tmp_path, monkeypatch):
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path))
    atoms = tmp_path / "atoms"
    atoms.mkdir()
    (tmp_path / "query.py").write_text("# stub\n", encoding="utf-8")
    (atoms / "ranked-association-map.json").write_text("{}", encoding="utf-8")
    (atoms / "alias-table.json").write_text("{}", encoding="utf-8")
    msg = index_compute_lab.write_graph_note("Sample Project", "Example contractor awarded the project.")
    assert "agent-notes" in msg
    note = atoms / "agent-notes" / "sample-project.md"
    assert note.is_file()
    assert "Sample Project" in note.read_text(encoding="utf-8")
