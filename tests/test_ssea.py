"""SSEA unit contracts plus real pinned REA/Seatbelt denial and fixture tests."""
import hashlib
import importlib.util
import json
import marshal
import os
import socket
from pathlib import Path
import struct
import sys
import threading
import time

import pytest

from algo_cli import ssea, ssea_sandbox as boundary, tools
from algo_cli.chatgpt_client import _build_responses_tools
from algo_cli.config import Config, CONFIG_DIR
from algo_cli.irene_memory_path_policy import ProtectedMemoryPathError, protected_tool_policy_error
from algo_cli.marcus_authority import CURATED_TOOL_POLICIES, EffectClass
from algo_cli.tool_context import select_tools_for_prompt

NAMES = ("ssea_status", "ssea_catalog", "ssea_analyze", "ssea_investigate")


def elf_bytes():
    return b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8) + struct.pack(
        "<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0)


def pe_bytes():
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3c, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x8664, 0, 0, 0, 0, 240, 0x22)
    struct.pack_into("<H", data, 0x98, 0x20b)
    struct.pack_into("<Q", data, 0x98 + 24, 0x140000000)
    struct.pack_into("<II", data, 0x98 + 32, 4096, 512)
    struct.pack_into("<II", data, 0x98 + 56, 4096, 512)
    struct.pack_into("<I", data, 0x98 + 108, 16)
    return bytes(data)


def pyc_bytes():
    return (importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, 0) +
            marshal.dumps(compile("def add(a, b): return a + b\n", "fixture.py", "exec")))


@pytest.fixture
def live():
    available = sys.platform == "darwin" and (Path.home() / ".local/share/rea/lib/node_modules/rea-agents/package.json").is_file()
    if not available:
        if os.environ.get("ALGO_TEST_REQUIRE_SSEA") == "1":
            pytest.fail("required real SSEA runtime unavailable")
        pytest.skip("pinned macOS runtime not installed")
    return Config(continuum_enabled=True)


@pytest.mark.parametrize("name", NAMES)
def test_registration_schema_and_approval(name):
    converted = _build_responses_tools([tools.TOOL_MAP[name]])
    assert converted and converted[0]["name"] == name
    assert "cfg" not in converted[0]["parameters"]["properties"]
    policy = CURATED_TOOL_POLICIES[name]
    if name in {"ssea_analyze", "ssea_investigate"}:
        assert policy.effect_class == EffectClass.CODE_EXECUTION
        assert policy.requires_approval and policy.maximum_risk == "high"
    else:
        assert not policy.requires_approval


def test_specialist_gating():
    names = {t.__name__ for t in select_tools_for_prompt("Fix the parser and run tests", tools.ALL_TOOLS)}
    assert not (set(NAMES) & names)


def test_dispatch_binds_workspace_and_passes_host_config(tmp_path, monkeypatch):
    from algo_cli.james_dispatch import dispatch_action
    from algo_cli.nathan_runtime import run_tool
    from test_james_dispatch import _dependencies

    launch = tmp_path.resolve() / "launch"
    workspace = tmp_path.resolve() / "workspace"
    for directory in (launch, workspace):
        directory.mkdir()
        (directory / "fixture.pyc").write_bytes(directory.name.encode())
    monkeypatch.chdir(launch)
    cfg = Config(cwd=str(workspace), ssea_install_root=str(workspace))
    checked = []

    def verify(received, _deadline):
        checked.append(received)
        return workspace, workspace

    def execute(operation, values, root, node, scratch, identity, received, deadline):
        assert checked == [cfg] and received is cfg
        assert (scratch / "target.bin").read_bytes() == b"workspace"
        return {"ok": True, "status": "executed", "input": identity}

    monkeypatch.setattr(boundary, "verify_runtime", verify)
    monkeypatch.setattr(ssea, "_execute", execute)
    deps = _dependencies(tmp_path, run_tool)
    approved = []

    def approve(_name, _args, *_a, preflight, **_kw):
        approved.append(preflight.signature_args["target"])
        return True

    deps.approve = approve
    result = dispatch_action("ssea_analyze", {"target": "fixture.pyc", "operation": "inspect_python_pyc"},
                             cfg, dependencies=deps, render=False)
    target = str(workspace / "fixture.pyc")
    assert result.outcome.worked
    assert approved == [target]
    assert json.loads(result.message["content"])["input"]["target"] == target


@pytest.mark.parametrize("contents", ["[]", "null", "{}", '{"name":"another","version":"6.8.0"}',
                                       '{"name":"rea-agents","version":1}'])
def test_status_rejects_invalid_package_metadata(tmp_path, monkeypatch, contents):
    root = tmp_path.resolve()
    (root / "package.json").write_text(contents, encoding="utf-8")
    monkeypatch.setattr(boundary, "installation", lambda _cfg: (root, root))
    result = ssea.status()
    assert not result["ok"] and result["error"] == "ssea_runtime_metadata_unavailable"


def test_status_reports_configured_package_version_as_metadata_only(tmp_path, monkeypatch):
    from algo_cli.nathan_runtime import run_tool

    root = tmp_path.resolve()
    cfg = Config(ssea_install_root=str(root))
    (root / "package.json").write_text('{"name":"rea-agents","version":"6.8.0"}', encoding="utf-8")

    def installation(received):
        assert received is cfg
        return root, root

    monkeypatch.setattr(boundary, "installation", installation)
    result = json.loads(run_tool("ssea_status", {}, cfg))
    assert result["ok"] and result["installed_version"] == "6.8.0"
    assert result["package_evidence"] == "metadata_only_not_readiness"


@pytest.mark.parametrize("stopped", [False, True])
def test_large_workflow_keeps_outcome_and_valid_model_json(tmp_path, monkeypatch, stopped):
    from algo_cli.james_dispatch import dispatch_action
    from algo_cli.nathan_runtime import TOOL_RESULT_CONTENT_LIMIT, run_tool
    from test_james_dispatch import _dependencies

    root = tmp_path.resolve()
    target = root / "fixture.pyc"
    target.write_bytes(b"synthetic input")
    calls = []

    def execute(operation, values, runtime_root, node, scratch, identity, cfg, deadline):
        calls.append(operation)
        if stopped and len(calls) == 3:
            return {"ok": False, "status": "provider_error", "error": "ssea_upstream_process_failed"}
        return {"ok": True, "status": "executed", "operation": operation, "input": identity,
                "evidence": {"evidence_id": "fixture-id", "observation": "x" * 40_000}}

    monkeypatch.setattr(boundary, "verify_runtime", lambda *_: (root, root))
    monkeypatch.setattr(ssea, "_execute", execute)
    deps = _dependencies(root, run_tool)
    deps.approve = lambda *_a, **_kw: True
    result = dispatch_action(
        "ssea_investigate", {"target": str(target), "steps": [{"operation": "inspect_python_pyc"}] * 4},
        Config(cwd=str(root), reflex_enabled=True), dependencies=deps, render=False,
    )
    assert result.outcome.worked is (not stopped)
    content = result.message["content"]
    assert len(content) <= TOOL_RESULT_CONTENT_LIMIT
    payload = json.loads(content)
    assert payload["ok"] is (not stopped)
    assert payload["status"] == ("stopped" if stopped else "executed")
    assert payload["detail_omitted"] is True
    assert payload["dispatch_status"] == result.outcome.status.value
    assert payload["steps"][0]["evidence_id"] == "fixture-id"
    if stopped:
        assert payload["stopped_at_step"] == 2
        assert payload["steps"][2]["error"] == "ssea_upstream_process_failed"


def test_parser_evidence_does_not_set_dispatch_status():
    from algo_cli.nathan_runtime import classify_tool_status

    result = ssea.serialize_result({"ok": True, "status": "executed",
                                    "evidence": {"observation": "[exit code: 7]"}})
    assert classify_tool_status(result, name="ssea_analyze") == "worked"
    assert classify_tool_status('{"ok":true', name="ssea_analyze") == "failed"


@pytest.mark.parametrize("operation,options", [
    ("run_shell", {}), ("inspect_format", {}), ("inspect_python_pyc", {"argv": []}),
    ("inspect_python_pyc", {"max_depth": True}), ("inspect_python_pyc", {"max_depth": 65}),
    ("inspect_python_pyc", {"max_depth": "12"}), ("inspect_elf_notes", []),
    ("inspect_pe_imports", {"max_dlls": 0}), ("inspect_native_exports", {"max_file_bytes": 2**32}),
])
def test_invalid_plan_never_enters_boundary(monkeypatch, operation, options):
    def forbidden(*args):
        pytest.fail("invalid plan entered execution boundary")
    monkeypatch.setattr(boundary, "verify_runtime", forbidden)
    assert not ssea.analyze(operation, "/missing", options)["ok"]


def test_workflow_validates_all_steps_before_launch(monkeypatch):
    monkeypatch.setattr(boundary, "verify_runtime", lambda *args: pytest.fail("invalid plan launched"))
    for steps in ([], [{}] * 5, [{"operation": "inspect_elf_notes", "shell": "bad"}],
                  [{"operation": "inspect_elf_notes"}, {"operation": "disassemble"}]):
        assert not ssea.investigate("/missing", steps)["ok"]


def test_snapshot_denies_aliases_special_and_protected(tmp_path):
    root = tmp_path.resolve()
    source = root / "input"
    source.write_bytes(b"safe")
    output = root / "copy"
    for raw in (str(CONFIG_DIR / "continuum/x"), "/dev/null"):
        with pytest.raises((ProtectedMemoryPathError, boundary.BoundaryError, OSError)):
            boundary.snapshot(raw, output, str(root))
    alias = root / "alias"
    alias.symlink_to(source)
    with pytest.raises(ProtectedMemoryPathError):
        boundary.snapshot(str(alias), output, str(root))
    ancestor = root / "ancestor"
    ancestor.symlink_to(root, target_is_directory=True)
    with pytest.raises(ProtectedMemoryPathError):
        boundary.snapshot(str(ancestor / "input"), output, str(root))
    hard = root / "hard"
    os.link(source, hard)
    with pytest.raises(ProtectedMemoryPathError):
        boundary.snapshot(str(hard), output, str(root))
    assert not output.exists()


def test_snapshot_rejects_ancestor_replacement_race(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    selected = root / "selected"
    selected.mkdir()
    (selected / "target").write_bytes(b"selected")
    unselected = root / "unselected"
    unselected.mkdir()
    (unselected / "target").write_bytes(b"not selected")
    real_check = boundary.require_allowed_path
    def replace_after_check(*args, **kwargs):
        value = real_check(*args, **kwargs)
        selected.rename(root / "moved")
        selected.symlink_to(unselected, target_is_directory=True)
        return value
    monkeypatch.setattr(boundary, "require_allowed_path", replace_after_check)
    with pytest.raises(OSError):
        boundary.snapshot(str(selected / "target"), root / "copy", str(root))
    assert not (root / "copy").exists()


def test_snapshot_is_bounded_and_source_unchanged(tmp_path):
    root = tmp_path.resolve()
    source, copy = root / "input", root / "copy"
    source.write_bytes(b"selected bytes")
    identity = boundary.snapshot(str(source), copy, str(root))
    assert identity["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert source.read_bytes() == copy.read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o400
    with source.open("wb") as stream:
        stream.truncate(boundary.MAX_INPUT + 1)
    with pytest.raises(boundary.BoundaryError, match="input_too_large"):
        boundary.snapshot(str(source), root / "large-copy", str(root))


def test_protected_targets_preflight():
    cfg = Config(continuum_enabled=True)
    assert protected_tool_policy_error("ssea_analyze", {"target": str(CONFIG_DIR / "continuum/a")}, cfg)
    assert protected_tool_policy_error("ssea_investigate", {"target": str(CONFIG_DIR / "continuum/a")}, cfg)


def test_manifest_drift_fails_before_launch(monkeypatch, tmp_path, live):
    node = tmp_path / "node"
    node.write_bytes(b"not reviewed")
    monkeypatch.setattr(boundary, "installation", lambda cfg: (tmp_path, node))
    with pytest.raises(boundary.BoundaryError, match="node_integrity"):
        boundary.verify_runtime(live, time.monotonic() + 30)


@pytest.mark.parametrize("operation,fixture", [
    ("inspect_python_pyc", pyc_bytes), ("inspect_elf_notes", elf_bytes),
    ("inspect_pe_imports", pe_bytes), ("inspect_native_exports", pe_bytes),
])
def test_real_upstream_fixture(operation, fixture, tmp_path, live):
    source = tmp_path.resolve() / "input.bin"
    source.write_bytes(fixture())
    result = ssea.analyze(operation, str(source), cfg=live)
    assert result["status"] == "executed", result
    assert result["evidence"]["operation"] == operation
    assert result["evidence"]["subject"]["digest"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["cleanup"] == "process_reaped_scratch_removed"
    assert not Path(result["evidence"]["subject"]["local_path"]).exists()
    assert not result["upstream_result_verified"]


def test_real_composed_workflow_stops_on_error(tmp_path, live):
    source = tmp_path.resolve() / "input.pe"
    source.write_bytes(pe_bytes())
    result = ssea.investigate(str(source), [
        {"operation": "inspect_pe_imports"}, {"operation": "inspect_native_exports"},
    ], live)
    assert result["ok"] and len(result["steps"]) == 2, result
    stopped = ssea.investigate(str(source), [
        {"operation": "inspect_python_pyc"}, {"operation": "inspect_pe_imports"},
    ], live)
    assert stopped["status"] == "stopped" and stopped["stopped_at_step"] == 0
    assert len(stopped["steps"]) == 1


def test_real_containment_denies_unselected_reads_writes_network_and_fork(tmp_path, live):
    root, node = boundary.verify_runtime(live, time.monotonic() + 30)
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir(mode=0o700)
    unselected = tmp_path.resolve() / "unselected"
    unselected.write_text("synthetic private canary", encoding="utf-8")
    alias = scratch / "escape"
    alias.symlink_to(unselected)
    # No actual private-store content is used. Probe fixture denies by OS policy,
    # including symlink escape, child exec, TCP and Unix-domain sockets.
    script = """
const fs=require('node:fs'), cp=require('node:child_process'), net=require('node:net');
const denied = (fn) => {try {fn(); return false} catch(e) {return ['EPERM','EACCES'].includes(e.code)}};
const result={read:denied(()=>fs.readFileSync(PATH)), alias:denied(()=>fs.readFileSync(ALIAS)),
write:denied(()=>fs.writeFileSync(PATH,'bad')), env:!process.env.SSEA_SECRET,
fork:cp.spawnSync(process.execPath,['-e','process.exit(0)']).error?.code === 'EPERM'};
Promise.all([new Promise(r=>net.connect({host:'127.0.0.1',port:9}).on('error',e=>r(e.code==='EPERM'))),
new Promise(r=>net.connect({path:'/private/tmp/ssea-no-such.sock'}).on('error',e=>r(e.code==='EPERM')).on('connect',function(){this.destroy();r(false)}))])
.then(([tcp,unix])=>console.log(JSON.stringify({...result,tcp,unix})));
""".replace("PATH", json.dumps(str(unselected))).replace("ALIAS", json.dumps(str(alias)))
    socket_path = Path("/private/tmp") / f"ssea-probe-{os.getpid()}.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen(1)
    script = script.replace("/private/tmp/ssea-no-such.sock", str(socket_path))
    os.environ["SSEA_SECRET"] = "never inherited"
    try:
        result = boundary.run(root, node, scratch, [], live, time.monotonic() + 10, script=script)
    finally:
        del os.environ["SSEA_SECRET"]
        listener.close()
        socket_path.unlink()
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts and all(facts.values()), facts
    assert unselected.read_text() == "synthetic private canary"


def test_real_protected_deny_overrides_scratch_allow(tmp_path, live, monkeypatch):
    from dataclasses import replace
    root, node = boundary.verify_runtime(live, time.monotonic() + 30)
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir(mode=0o700)
    protected = scratch / "synthetic-memory"
    protected.mkdir()
    canary = protected / "canary"
    canary.write_text("synthetic protected fixture")
    rules = boundary.protected_path_rules()
    monkeypatch.setattr(boundary, "protected_path_rules",
                        lambda: replace(rules, roots=(*rules.roots, str(protected))))
    script = ("try{require('node:fs').readFileSync(" + json.dumps(str(canary)) +
              ");console.log('allowed')}catch(e){console.log(e.code)}")
    result = boundary.run(root, node, scratch, [], live, time.monotonic() + 5, script=script)
    assert result.returncode == 0 and result.stdout.strip() == b"EPERM"


def test_dispatch_requires_exact_approval_before_execution(tmp_path, live):
    from algo_cli.james_dispatch import dispatch_action
    from test_james_dispatch import _dependencies
    dependencies = _dependencies(tmp_path, lambda *_: pytest.fail("dispatch without approval"))
    dependencies.approve = lambda *_a, **_kw: False
    target = tmp_path.resolve() / "fixture"
    target.write_bytes(pe_bytes())
    result = dispatch_action("ssea_analyze", {"operation": "inspect_pe_imports", "target": str(target)},
                             live, dependencies=dependencies, render=False)
    assert not result.outcome.invoked


def test_real_timeout_output_and_cancellation_cleanup(tmp_path, live):
    root, node = boundary.verify_runtime(live, time.monotonic() + 30)
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir(mode=0o700)
    result = boundary.run(root, node, scratch, [], live, time.monotonic() + .4,
                          script="setInterval(()=>{},100)")
    assert result.timed_out and result.returncode != 0
    result = boundary.run(root, node, scratch, [], live, time.monotonic() + 5,
                          script="process.stdout.write('x'.repeat(1000000));setInterval(()=>{},100)")
    assert result.truncated and len(result.stdout) <= boundary.MAX_OUTPUT
    event = threading.Event()
    live._algo_team_cancellation = event
    timer = threading.Timer(.4, event.set)
    timer.start()
    try:
        result = boundary.run(root, node, scratch, [], live, time.monotonic() + 5,
                              script="setInterval(()=>{},100)")
        assert result.cancelled and result.returncode != 0
    finally:
        timer.cancel()
