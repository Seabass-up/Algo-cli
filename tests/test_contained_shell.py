"""Real protected command execution; no private stores or network services used."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from algo_cli import contained_shell as shell, nathan_runtime, tools
from algo_cli.config import Config
from algo_cli.session_mode import select_mode


@pytest.fixture
def cfg(tmp_path):
    if sys.platform != "darwin":
        import os
        if os.environ.get("ALGO_TEST_REQUIRE_CONTAINED_SHELL") == "1":
            pytest.fail("required command containment unavailable")
        pytest.skip("qualified macOS boundary")
    return Config(cwd=str(tmp_path.resolve()), continuum_enabled=True, safe_mode=False)


def invoke(command, cfg, timeout=10):
    return nathan_runtime.run_tool("run_shell", {"command": command, "timeout": timeout}, cfg)


def test_real_shell_python_and_subprocess(cfg):
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "import subprocess; subprocess.run(['/usr/bin/true'],check=True);print('real execution')")
    result = invoke(command, cfg)
    assert "real execution" in result and "[exit code: 0]" in result, result
    assert cfg.continuum_enabled is True


def test_real_python_tests_and_node_build(cfg):
    root = Path(cfg.cwd)
    (root / "test_fixture.py").write_text("def test_arithmetic(): assert 2 + 3 == 5\n")
    result = invoke(f"{shlex.quote(sys.executable)} -m pytest test_fixture.py -q", cfg, 20)
    assert "1 passed" in result and "[exit code: 0]" in result, result
    (root / "package.json").write_text(json.dumps({"scripts": {"build": "node build.js"}}))
    (root / "build.js").write_text("require('node:fs').writeFileSync('built.txt','real build');console.log('built')")
    result = invoke("npm run build --silent", cfg, 20)
    assert "[exit code: 0]" in result and (root / "built.txt").read_text() == "real build", result


def test_real_c_build_with_installed_developer_tools(cfg):
    root = Path(cfg.cwd)
    (root / "tiny.c").write_text("int answer(void) { return 42; }" + chr(10))
    result = invoke("cc -c tiny.c -o tiny.o", cfg, 20)
    assert "[exit code: 0]" in result and (root / "tiny.o").stat().st_size > 0, result


def test_scratch_alias_cannot_read_workspace_credentials(cfg):
    secret = Path(cfg.cwd) / ".env"
    secret.write_text("synthetic env secret")
    script = ("import os;from pathlib import Path;p=Path(os.environ['TMPDIR'])/'public';" +
              "p.symlink_to(" + repr(str(secret)) + ");print(p.read_text())")
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "synthetic env secret" not in result and "[exit code: 1]" in result, result


def test_inherited_limits_and_process_group_budget(cfg, monkeypatch):
    monkeypatch.setattr(shell, "MAX_GROUP_PROCESSES", 4)
    script = """
import subprocess,sys,time,errno,resource
limit=resource.getrlimit(resource.RLIMIT_NPROC)[1]
try:
 resource.setrlimit(resource.RLIMIT_NPROC,(limit+100,limit+100));print('RAISED HARD LIMIT')
except (ValueError,PermissionError): pass
children=[]
try:
 for _ in range(6):
  try: children.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)']))
  except OSError as e:
   if e.errno==errno.EAGAIN: print('fork limited');break
   raise
 time.sleep(.4)
finally:
 for p in children:
  p.terminate();p.wait()
"""
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "RAISED HARD LIMIT" not in result
    assert "process-group limit" in result or "fork limited" in result, result


def test_real_failed_test_preserves_failure(cfg):
    (Path(cfg.cwd) / "test_fail.py").write_text("def test_failure(): assert False\n")
    result = invoke(f"{shlex.quote(sys.executable)} -m pytest test_fail.py -q", cfg, 20)
    assert "1 failed" in result and "[exit code: 1]" in result, result


def test_yolo_preapproval_keeps_memory_and_runs_real_command(cfg):
    from algo_cli.nathan_runtime import ask_approval, preflight_runtime_tool
    select_mode(cfg, "yolo", user_initiated=True)
    args = {"command": "printf 'yolo real execution'"}
    checked = preflight_runtime_tool("run_shell", args, cfg)
    assert checked.allowed and ask_approval("run_shell", args, cfg, preflight=checked)
    result = nathan_runtime.run_tool("run_shell", args, cfg)
    assert "yolo real execution" in result and "[exit code: 0]" in result, result
    assert cfg.continuum_enabled is True


def test_model_cannot_override_runtime_config_workspace_or_environment(cfg, tmp_path):
    result = nathan_runtime.run_tool("run_shell", {
        "command": "pwd", "cfg": Config(continuum_enabled=False), "cwd": "/", "safe_mode": False,
    }, cfg)
    assert str(tmp_path.resolve()) in result and "contained workspace command" in result, result


@pytest.mark.parametrize("name", [".env", ".ENV", ".env.local", "credentials.json", "service-account.json", "id_rsa", "private.pem", ".npmrc"])
def test_real_credentials_denied_in_workspace(cfg, name):
    (Path(cfg.cwd) / name).write_text("synthetic credential canary")
    script = "from pathlib import Path;print(Path(" + repr(name) + ").read_text())"
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "synthetic credential canary" not in result and "[exit code: 1]" in result, result


def test_real_outside_and_protected_reads_writes_aliases_env_and_network_denied(cfg, tmp_path, monkeypatch):
    from dataclasses import replace
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("synthetic private canary")
    (Path(cfg.cwd) / "alias").symlink_to(outside)
    protected = Path(cfg.cwd) / "synthetic-memory"
    protected.mkdir()
    (protected / "entry").write_text("synthetic memory canary")
    rules = shell.protected_path_rules()
    monkeypatch.setattr(shell, "protected_path_rules", lambda: replace(rules, roots=(*rules.roots, str(protected))))
    monkeypatch.setenv("ALGO_SECRET_CANARY", "never inherit")
    script = """
import os,socket,json
from pathlib import Path
facts={'env':os.getenv('ALGO_SECRET_CANARY') is None}
for name,fn in {'outside':lambda:Path(OUTSIDE).read_text(),'alias':lambda:Path('alias').read_text(),
 'protected':lambda:Path(PROTECTED).read_text(),'write':lambda:Path(OUTSIDE).write_text('bad'),
 'link':lambda:os.link(OUTSIDE,'linked-outside'),
 'tcp':lambda:socket.create_connection(('127.0.0.1',9),timeout=1)}.items():
 try: fn();facts[name]=False
 except PermissionError: facts[name]=True
print(json.dumps(facts))
""".replace("OUTSIDE", repr(str(outside))).replace("PROTECTED", repr(str(protected / "entry")))
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert '"env": true' in result and all('"'+key+'": true' in result for key in ("outside","alias","protected","write","link","tcp")), result
    assert outside.read_text() == "synthetic private canary"


def test_preexisting_hardlink_cannot_be_read_or_renamed_out_of_deny(cfg, tmp_path):
    import os
    outside = tmp_path.parent / (tmp_path.name + "-secret")
    outside.write_text("synthetic hardlink secret")
    folder = Path(cfg.cwd) / "aliases"
    folder.mkdir()
    os.link(outside, folder / "public")
    script = """
from pathlib import Path
import os
for action in (lambda:Path('aliases/public').read_text(),lambda:os.rename('aliases','renamed')):
 try: print(action())
 except PermissionError: print('denied')
"""
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "synthetic hardlink secret" not in result and result.count("denied") >= 2, result


def test_protected_inode_alias_is_pruned_and_denied(cfg, monkeypatch):
    from dataclasses import replace
    import stat
    target = Path(cfg.cwd) / "aliased-store"
    target.mkdir()
    (target / "entry").write_text("synthetic inode-protected body")
    info = target.stat()
    rules = shell.protected_path_rules()
    monkeypatch.setattr(shell, "protected_path_rules",
                        lambda: replace(rules, identities=(*rules.identities,
                                        (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))))
    script = "from pathlib import Path;print(Path('aliased-store/entry').read_text())"
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "synthetic inode-protected body" not in result and "[exit code: 1]" in result, result


def test_cannot_read_other_process_environment(cfg):
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"],
                               env={"SYNTHETIC_SECRET": "do-not-expose-proc-env"})
    try:
        result = invoke(f"/bin/ps eww -p {process.pid} -o command=", cfg)
        assert "do-not-expose-proc-env" not in result, result
    finally:
        process.kill()
        process.wait(timeout=5)


def test_real_group_escape_denied_and_background_cleaned(cfg):
    root = Path(cfg.cwd)
    script = """
import os,subprocess,sys,time
for fn in (os.setsid,lambda:os.setpgid(0,0)):
 try: fn();print('ESCAPED')
 except PermissionError: print('escape denied')
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
open('child.pid','w').write(str(p.pid))
print('spawned child')
"""
    result = invoke(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", cfg)
    assert "ESCAPED" not in result and result.count("escape denied") == 2 and "[exit code: 0]" in result, result
    pid = int((root / "child.pid").read_text())
    # A terminated child may briefly remain as an init-owned zombie; it must not
    # remain a running worker after the shell exits successfully.
    for _ in range(30):
        state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(.05)
    assert not state or state.startswith("Z"), state


def test_timeout_cancel_output_are_not_success(cfg):
    code = shlex.quote("import time;time.sleep(30)")
    command = f"{shlex.quote(sys.executable)} -c {code}"
    assert "timed out" in invoke(command, cfg, .3)
    event = threading.Event()
    cfg._algo_team_cancellation = event
    timer = threading.Timer(.3, event.set)
    timer.start()
    try:
        assert "cancelled" in invoke(command, cfg)
    finally:
        timer.cancel()
    event.clear()
    noisy = shlex.quote("print('x'*1000000)")
    result = invoke(f"{shlex.quote(sys.executable)} -c {noisy}", cfg)
    assert "output limit" in result and "[exit code: 0]" not in result


def test_safe_and_destructive_and_unsupported_hosts_still_deny(cfg, monkeypatch):
    assert "destructive-action" in invoke("rm -rf anything", cfg)
    cfg.safe_mode = True
    assert "Blocked" in invoke("touch marker", cfg)
    assert not (Path(cfg.cwd) / "marker").exists()
    monkeypatch.setattr(shell.sys, "platform", "win32")
    assert "qualified macOS containment" in invoke("true", cfg)


def test_schema_hides_all_runtime_authority():
    from algo_cli.chatgpt_client import _build_responses_tools
    tool = _build_responses_tools([tools.TOOL_MAP["run_shell"]])[0]
    assert set(tool["parameters"]["properties"]) == {"command", "timeout"}
