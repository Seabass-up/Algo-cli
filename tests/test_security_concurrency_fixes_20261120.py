"""Regression tests for Critical & High security and concurrency fixes."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Ensure repo root is importable
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if "ollama" not in sys.modules:
    try:
        import ollama  # noqa: F401
    except ImportError:
        import types
        _stub = types.ModuleType("ollama")
        _stub.Client = object
        _stub.ResponseError = Exception
        sys.modules["ollama"] = _stub


class TestIrenePathPolicy(unittest.TestCase):
    def test_nonexistent_dotdot_symlink_bypass_blocked(self):
        from algo_cli.irene_memory_path_policy import (
            ProtectedMemoryPathError,
            ProtectedPathRules,
            require_allowed_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspace"
            secret_dir = root / "secret_memory"
            ws.mkdir()
            secret_dir.mkdir()
            secret_file = secret_dir / "private.txt"
            secret_file.write_text("top-secret", encoding="utf-8")
            link = ws / "alias_link"
            os.symlink(secret_file, link)

            rules = ProtectedPathRules(
                roots=(str(secret_dir),),
                residue_parent=str(root).casefold(),
                residue_prefixes=(),
                identities=(),
            )
            traversal = ws / "nonexistent" / ".." / "alias_link"
            with self.assertRaises(ProtectedMemoryPathError):
                require_allowed_path(traversal, cwd=ws, rules=rules)


class TestFlowDagSecurityAndTemplates(unittest.TestCase):
    def test_python_node_blocks_imports_and_dunder_escape(self):
        from algo_cli.intelligence.flow_dag import FlowDefinition, FlowExecutor, FlowNode

        for bad_code in (
            "import os\nresult = os.getuid()",
            "result = __import__('os').getuid()",
            "result = ().__class__.__bases__[0].__subclasses__()",
        ):
            flow = FlowDefinition(
                name="evil",
                nodes=[FlowNode(id="n1", kind="python", source_code=bad_code)],
                outputs={"out": "${n1.text}"},
            )
            res = FlowExecutor().run(flow)
            self.assertFalse(res.success)
            self.assertIsNotNone(res.traces[0].error)

    def test_python_node_allows_safe_computation(self):
        from algo_cli.intelligence.flow_dag import FlowDefinition, FlowExecutor, FlowNode

        flow = FlowDefinition(
            name="ok",
            nodes=[FlowNode(id="n1", kind="python", source_code="result = sum([1, 2, 3])")],
            outputs={"out": "${n1.text}"},
        )
        res = FlowExecutor().run(flow)
        self.assertTrue(res.success)
        self.assertEqual(res.outputs["out"], "6")

    def test_multi_reference_dependencies_cycles_and_resolution(self):
        from algo_cli.intelligence.flow_dag import (
            FlowDefinition,
            FlowExecutor,
            FlowNode,
            detect_cycles,
            _resolve_value,
        )

        # Cycle routed through second ${...} reference must be detected
        cyclic = FlowDefinition(
            name="cyc",
            nodes=[
                FlowNode(id="c", kind="tool", tool="t"),
                FlowNode(id="a", kind="tool", tool="t", inputs={"x": "${c.text} + ${b.text}"}),
                FlowNode(id="b", kind="tool", tool="t", inputs={"x": "${a.text}"}),
            ],
        )
        self.assertIsNotNone(detect_cycles(cyclic))

        # Multi-ref expansion & missing field handling
        ctx = {"a": {"text": "alpha"}, "b": {"text": "beta"}}
        self.assertEqual(_resolve_value("${a.text} + ${b.text}", ctx), "alpha + beta")
        self.assertEqual(_resolve_value("hello ${a.missing}!", ctx), "hello !")


class TestExecutionGuardrailsBypasses(unittest.TestCase):
    def test_inline_python_unspaced_operator_rejected(self):
        from algo_cli.execution_guardrails import classify_verification_command

        for cmd in (
            "python3 -c 'assert False;#'||true",
            "python3 -c 'assert False;#';true",
            "python3 -c 'assert False;#'|cat",
        ):
            decision = classify_verification_command(cmd)
            self.assertFalse(decision.qualifies, f"Should reject: {cmd}")

        # Legitimate inline assertion with semicolon inside quotes still qualifies
        good = classify_verification_command("python3 -c 'x = 1; assert x == 1'")
        self.assertTrue(good.qualifies)

    def test_cd_prefix_injection_rejected(self):
        from algo_cli.execution_guardrails import classify_verification_command

        for cmd in (
            "cd /tmp;echo_evil && pytest",
            "cd $(id) && pytest",
            "cd /tmp;echo_evil && pytest && ruff check",
        ):
            decision = classify_verification_command(cmd)
            self.assertFalse(decision.qualifies, f"Should reject: {cmd}")


class TestSafeModeGitConfig(unittest.TestCase):
    def test_dangerous_git_c_and_config_env_blocked(self):
        from algo_cli.tools import shell_is_dangerous

        for cmd in (
            "git -c core.fsmonitor=/tmp/helper status",
            "git -c core.pager=/tmp/helper log",
            "git -c diff.external=/tmp/helper diff",
            "git --config-env=core.fsmonitor=HELPER status",
        ):
            self.assertTrue(shell_is_dangerous(cmd), f"Should block: {cmd}")

        # Benign allowlisted display config remains allowed
        self.assertFalse(shell_is_dangerous("git -c color.ui=false --no-pager diff --stat"))


class TestBoronAndXenonIsolation(unittest.TestCase):
    def test_docker_verifier_rejects_unconfined_and_root_aliases(self):
        from algo_cli.boron_browser_isolation import BoronIsolationRejected, _assert_no_host_surface

        base_inspect = {
            "HostConfig": {
                "Binds": [], "PortBindings": {}, "PublishAllPorts": False,
                "NetworkMode": "boron-net",
                "ReadonlyRootfs": True,
                "Privileged": False,
                "IpcMode": "private",
                "PidMode": "",
                "UTSMode": "",
                "UsernsMode": "",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=true", "seccomp=/etc/seccomp.json"],
                "PidsLimit": 64,
                "Memory": 64 * 1024 * 1024,
                "AutoRemove": True,
                "RestartPolicy": {"Name": "no"},
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,nodev,size=67108864",
                    "/run": "rw,noexec,nosuid,nodev,size=16777216",
                },
            },
            "Mounts": [], "NetworkSettings": {"Networks": {"boron-net": {}}, "Ports": {}},
            "Config": {"User": "1001:1001"},
        }
        # Baseline valid broker passes
        _assert_no_host_surface(base_inspect, broker=True, network_name="boron-net")

        # seccomp=unconfined must fail
        bad_seccomp = {**base_inspect, "HostConfig": {**base_inspect["HostConfig"], "SecurityOpt": ["no-new-privileges=true", "seccomp=unconfined"]}}
        with self.assertRaises(BoronIsolationRejected):
            _assert_no_host_surface(bad_seccomp, broker=True, network_name="boron-net")

        # Root aliases must fail
        for bad_user in ("root:1000", "root:root", "00:1000", "1000:0"):
            bad_cfg = {**base_inspect, "Config": {"User": bad_user}}
            with self.assertRaises(BoronIsolationRejected, msg=f"Should reject user {bad_user}"):
                _assert_no_host_surface(bad_cfg, broker=True, network_name="boron-net")

    def test_xenon_strict_content_length_parsing(self):
        from algo_cli.xenon_browser_broker import XenonBrokerRejected, parse_xenon_http_response

        for bad_len in (b"1_0", b"+10", b"0x10", b"-1"):
            head = b"HTTP/1.1 200 OK\r\nContent-Length: " + bad_len + b"\r\n\r\n"
            with self.assertRaises(XenonBrokerRejected, msg=f"Should reject Content-Length {bad_len!r}"):
                parse_xenon_http_response(head)


class TestPolicyChain(unittest.TestCase):
    def test_sufficient_semantics_path_traversal_and_include_cycle(self):
        from algo_cli._internal.policy_chain import (
            CheckResult,
            Control,
            evaluate_chain,
            path_allowlist_check,
        )

        # Failing SUFFICIENT should not fail the chain when REQUIRED passes
        def fail_suff(_call, _ctx):
            return CheckResult(name="s", passed=False, reason="not matched")

        def pass_req(_call, _ctx):
            return CheckResult(name="r", passed=True)

        decision = evaluate_chain(
            "main",
            [(Control.SUFFICIENT, fail_suff), (Control.REQUIRED, pass_req)],
            {},
        )
        self.assertTrue(decision.passed)

        # Path traversal and sibling prefix bypass blocked
        chk = path_allowlist_check(allow=["/allowed"], block=["/etc"])
        self.assertFalse(chk({"path": "/allowed/../etc/passwd"}, {}).passed)
        self.assertFalse(chk({"path": "/allowed_other/secret"}, {}).passed)
        self.assertTrue(chk({"path": "/allowed/sub/file.txt"}, {}).passed)

        # Cyclic INCLUDE terminates without RecursionError
        def inc_a(_call, _ctx):
            return CheckResult(name="a", passed=True)
        inc_a.__name__ = "a"

        def registry(name):
            return [(Control.INCLUDE, inc_a)] if name == "a" else None

        cyc_decision = evaluate_chain("a", [(Control.INCLUDE, inc_a)], {}, registry=registry)
        self.assertFalse(cyc_decision.passed)
        self.assertTrue(any("cycle" in r.reason for r in cyc_decision.results))


class TestMemoryBatchAndAlgorithms(unittest.TestCase):
    def test_memory_batch_releases_lock_on_begin_failure(self):
        from algo_cli.intelligence.autonomous_engineer import MemoryEngine

        with tempfile.TemporaryDirectory() as tmp:
            me = MemoryEngine(str(Path(tmp) / "mem.db"))
            try:
                me.conn.execute("BEGIN")
                with self.assertRaises(sqlite3.OperationalError):
                    with me.batch():
                        pass
                # Lock must NOT be leaked
                acquired = []
                def probe():
                    ok = me._lock.acquire(blocking=False)
                    if ok:
                        me._lock.release()
                    acquired.append(ok)
                t = threading.Thread(target=probe)
                t.start()
                t.join(timeout=2)
                self.assertEqual(acquired, [True])
            finally:
                me.conn.execute("ROLLBACK")
                me.close()

    def test_hyperloglog_64bit_no_crash_and_leading_zeros(self):
        from algo_cli.intelligence.hyperloglog import HyperLogLog

        hll = HyperLogLog(precision=4)
        self.assertEqual(hll._count_leading_zeros(0, 60), 61)
        self.assertEqual(hll._count_leading_zeros(1, 60), 60)
        for i in range(len(hll._registers)):
            hll._registers[i] = 35
        est = hll.estimate()
        self.assertGreater(est, 1 << 32)

    def test_combinatorial_hubo_to_qubo_and_empty_inputs(self):
        from algo_cli.reasoning.combinatorial import (
            HuboProblem,
            build_hubo_from_fragments,
            simulated_annealing,
        )

        # Empty inputs do not crash
        empty = build_hubo_from_fragments([])
        self.assertEqual(empty.n_variables, 0)
        self.assertEqual(simulated_annealing(empty), ([], 0.0))

        # Two negative similarities must NOT produce positive 3-body coherence
        prob = build_hubo_from_fragments(
            ["a", "b", "c"],
            embeddings=[[1.0, 0.0], [-1.0, 0.0], [-1.0, 0.0]],
            order=3,
        )
        self.assertNotIn((0, 1, 2), prob.weights)

        # QUBO reduction preserves objective on valid states and penalizes invalid auxiliary states
        hubo = HuboProblem(n_variables=3, weights={(0, 1, 2): 1.0})
        qubo = hubo.to_qubo()

        def eval_qubo(x):
            return sum(w * x[i] * x[j] for (i, j), w in qubo.items())

        # Valid states: x3 == x0 * x1
        self.assertAlmostEqual(eval_qubo([1, 1, 1, 1]), 1.0)
        self.assertAlmostEqual(eval_qubo([0, 0, 0, 0]), 0.0)
        # Invalid auxiliary state (x0=0, x1=0, x2=1, x3=1) must be strictly penalized (> objective)
        self.assertGreater(eval_qubo([0, 0, 1, 1]), eval_qubo([1, 1, 1, 1]))


class TestNoFdDoubleClose(unittest.TestCase):
    def test_atomic_write_helpers_do_not_double_close_fd_on_error(self):
        from algo_cli.henry_effect_control import TargetLeaseManager

        with tempfile.TemporaryDirectory() as tmp:
            mgr = TargetLeaseManager(Path(tmp))
            target = Path(tmp) / "lease.json"
            closed_fds: list[int] = []
            real_close = os.close

            def tracking_close(fd: int) -> None:
                closed_fds.append(fd)
                real_close(fd)

            with (
                mock.patch("os.close", side_effect=tracking_close),
                mock.patch("os.replace", side_effect=OSError("simulated replace failure")),
            ):
                with self.assertRaises(OSError):
                    mgr._write_state(target, {"owner_id": "a", "issued_at": 1, "expires_at": 2})
            # os.fdopen already closed the fd; os.close must NOT be called a second time
            self.assertEqual(closed_fds, [])


if __name__ == "__main__":
    unittest.main()
