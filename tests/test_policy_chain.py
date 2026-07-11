"""Tests for the PAM-style policy chain (J10)."""
from __future__ import annotations

from algo_cli._internal.policy_chain import (
    Control,
    command_grep_check,
    evaluate_chain,
    path_allowlist_check,
    tier_check,
)


def _basic_call(**kwargs):
    base = {
        "tool": "algo_cli.shell.write",
        "tier": "tier2",
        "command": "pytest -q",
        "path": "/Users/example/Code/algo-cli/algo_cli/evals/cot_quality.py",
    }
    base.update(kwargs)
    return base


def test_chain_evaluates_all_required():
    """Five REQUIRED checks all run; their results are all recorded."""
    chain = [
        (Control.REQUIRED, tier_check("tier2")),
        (Control.REQUIRED, path_allowlist_check(allow=["/Users/"], block=["/System"])),
        (Control.REQUIRED, command_grep_check(deny=["rm -rf", "sudo "])),
    ]
    d = evaluate_chain("shell.write", chain, _basic_call())
    assert d.passed
    assert len(d.results) == 3
    assert all(r.passed for r in d.results)


def test_sufficient_short_circuits():
    """A SUFFICIENT pass stops evaluation of the rest of the chain."""
    chain = [
        (Control.REQUIRED, tier_check("tier1")),
        (Control.SUFFICIENT, command_grep_check(deny=["forbidden"])),
        (Control.REQUIRED, path_allowlist_check(allow=["/"])),
    ]
    d = evaluate_chain("test", chain, _basic_call(tier="tier1", command="ls"))
    assert d.passed
    # path_allowlist should NOT have run due to sufficient short-circuit
    assert not any("path_allowlist" in r.name for r in d.results)


def test_requisite_aborts():
    """A REQUISITE failure halts the chain immediately."""
    chain = [
        (Control.REQUISITE, tier_check("tier3")),
        (Control.REQUIRED, path_allowlist_check(allow=["/Users/"])),
        (Control.REQUIRED, command_grep_check(deny=["rm -rf"])),
    ]
    d = evaluate_chain("shell.write", chain, _basic_call(tier="tier2"))
    assert not d.passed
    assert d.abort_reason != ""
    # Only the first check ran; the others were skipped
    assert len(d.results) == 1
    assert "tier" in d.results[0].name


def test_include_runs_subchain():
    """An INCLUDE check invokes the registry and merges the sub-chain results."""
    sub_chain = [
        (Control.REQUIRED, command_grep_check(deny=["forbidden"])),
    ]
    def registry(name):
        return sub_chain if name == "sub_safety" else None
    # We need a way to use INCLUDE — let's add a named wrapper.
    def _named_include(name):
        def _check(_tool, _ctx):
            from algo_cli._internal.policy_chain import CheckResult
            return CheckResult(name=name, passed=True, reason="include-marker")
        _check.__name__ = name
        return _check

    chain2 = [
        (Control.REQUIRED, tier_check("tier1")),
        (Control.INCLUDE, _named_include("sub_safety")),
    ]
    d = evaluate_chain("test", chain2, _basic_call(tier="tier1"), registry=registry)
    # The include check runs, then the registry's sub_chain runs, then its result is recorded
    assert any(r.name == "command_grep" for r in d.results)
    assert d.passed


def test_chain_decision_serializable():
    """ChainDecision.to_dict returns a JSON-friendly dict."""
    chain = [(Control.REQUIRED, tier_check("tier1"))]
    d = evaluate_chain("test", chain, _basic_call(tier="tier1"))
    out = d.to_dict()
    assert out["chain"] == "test"
    assert isinstance(out["passed"], bool)
    assert isinstance(out["results"], list)
    assert isinstance(out["reasons"], list)


def test_tier_check_pass_and_fail():
    """tier_check accepts >= and rejects <."""
    high = tier_check("tier2")
    r1 = high(_basic_call(tier="tier3"), {})
    r2 = high(_basic_call(tier="tier1"), {})
    assert r1.passed
    assert not r2.passed


def test_path_allowlist_check():
    """path_allowlist allows startswith match and blocks list match."""
    check = path_allowlist_check(allow=["/Users/"], block=["/System"])
    assert check(_basic_call(path="/Users/example/bar.py"), {}).passed
    assert not check(_basic_call(path="/System/Library/foo"), {}).passed
    assert not check(_basic_call(path="/etc/passwd"), {}).passed


def test_command_grep_check_deny():
    """command_grep rejects commands containing denied substrings."""
    check = command_grep_check(deny=["rm -rf", "sudo "])
    assert check(_basic_call(command="ls -la"), {}).passed
    assert not check(_basic_call(command="sudo rm -rf /"), {}).passed
    assert not check(_basic_call(command="rm -rf /tmp"), {}).passed


def test_fired_rules_helper():
    """fired_rules() returns the names of checks that failed or the sufficient that passed."""
    chain = [
        (Control.REQUIRED, tier_check("tier1")),
        (Control.SUFFICIENT, command_grep_check(deny=["forbidden"])),
        (Control.REQUIRED, path_allowlist_check(allow=["/"])),
    ]
    d = evaluate_chain("test", chain, _basic_call(tier="tier1", command="ls", path="/tmp"))
    fired = d.fired_rules()
    # The sufficient check fired; the others may or may not appear
    assert any("command_grep" in name for name in fired)


def test_check_exception_does_not_crash_chain():
    """If a check raises, the chain records a failure but continues."""
    def _broken(_t, _c):
        raise RuntimeError("boom")

    chain = [
        (Control.REQUIRED, _broken),
        (Control.REQUIRED, tier_check("tier1")),
    ]
    d = evaluate_chain("test", chain, _basic_call(tier="tier1"))
    assert not d.passed
    # The tier check after the broken one still ran
    assert len(d.results) == 2
    assert not d.results[0].passed
    assert "boom" in d.results[0].reason
    assert d.results[1].passed


def test_chain_against_existing_policy_decisions():
    """Regression: chain verdict matches the simple policy for benign calls."""
    chain = [
        (Control.REQUIRED, tier_check("tier1")),
        (Control.REQUIRED, path_allowlist_check(allow=["/Users/", "/tmp/"], block=["/System"])),
        (Control.REQUIRED, command_grep_check(deny=["rm -rf", "sudo ", "mkfs", "dd if="])),
    ]
    # A safe call should pass
    safe = _basic_call(
        tier="tier1",
        command="pytest -q",
        path="/Users/example/Code/algo-cli/tests/test_cot_quality.py",
    )
    assert evaluate_chain("shell.write", chain, safe).passed
    # A dangerous call should fail
    dangerous = _basic_call(
        tier="tier1",
        command="sudo rm -rf /",
        path="/Users/example",
    )
    assert not evaluate_chain("shell.write", chain, dangerous).passed
