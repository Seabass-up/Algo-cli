"""PAM-style policy chain (J10).

A chain of named, composable checks with control flags
(required / sufficient / requisite / include), modeled on the
macOS PAM stack (/etc/pam.d/*).

Provenance: ALGO.md J10 (PAM-style policy chain), J5 (SHAuthorizationRight),
J13 (flat-text policy file). The chain is the upgrade path for
algo_cli/tool_policy.py from a single boolean to a composable,
audit-trail-preserving decision model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence


class Control(str, Enum):
    """PAM-style control flags.

    - REQUIRED: must pass; failure is recorded but the chain continues
      to gather full evidence (PAM "required" semantics).
    - SUFFICIENT: pass short-circuits the chain to PASS unless an earlier
      required check already failed; a failure is ignored (PAM "sufficient").
    - REQUISITE: must pass; failure aborts the chain immediately
      (PAM "requisite" semantics — fail-fast).
    - INCLUDE: recursively evaluate a named chain. The chain name is
      read from the check's `name` field (PAM "include" semantics).
    """

    REQUIRED = "required"
    SUFFICIENT = "sufficient"
    REQUISITE = "requisite"
    INCLUDE = "include"


@dataclass
class CheckResult:
    """Outcome of one check inside a chain."""

    name: str
    passed: bool
    reason: str = ""
    control: Control = Control.REQUIRED
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "control": self.control.value,
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass
class ChainDecision:
    """Aggregate outcome of evaluating one named chain."""

    chain: str
    passed: bool
    results: tuple[CheckResult, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    abort_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "chain": self.chain,
            "passed": self.passed,
            "abort_reason": self.abort_reason,
            "results": [r.to_dict() for r in self.results],
            "reasons": list(self.reasons),
        }

    def fired_rules(self) -> list[str]:
        """Return the names of checks that failed (or the `sufficient` that short-circuited)."""
        return [r.name for r in self.results if not r.passed or r.control == Control.SUFFICIENT]


# A Check is a callable that returns a CheckResult.
Check = Callable[[dict[str, Any], dict[str, Any]], CheckResult]

# A Chain is an ordered list of (control, check) pairs.
Chain = Sequence[tuple[Control, Check]]

# get_chain resolves an include reference to its chain definition.
ChainRegistry = Callable[[str], Chain | None]


def evaluate_chain(
    chain_name: str,
    checks: Chain,
    tool_call: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    registry: ChainRegistry | None = None,
) -> ChainDecision:
    """Evaluate a chain of checks against a tool call.

    Args:
        chain_name: Display name for this chain.
        checks: Ordered (control, check) pairs.
        tool_call: The tool invocation being gated. Shape:
            {"tool": "algo_cli.shell.write", "args": {...}, "tier": "tier2", ...}
        context: Session-level state (e.g. cwd, env, user).
        registry: Optional callback that returns a named chain for INCLUDE.

    Returns:
        A ChainDecision with per-check results, the overall verdict, and
        structured reasons suitable for the audit log.
    """
    ctx = context or {}
    visited = set(ctx.get("_policy_chain_visited", ()))
    visited.add(chain_name)
    results: list[CheckResult] = []
    abort_reason = ""
    prior_required_failed = False

    for control, check in checks:
        # INCLUDE — recursively evaluate a registered chain.
        if control == Control.INCLUDE:
            include_name = getattr(check, "__name__", "<include>")
            if include_name in visited:
                prior_required_failed = True
                results.append(CheckResult(
                    name=include_name,
                    passed=False,
                    reason=f"include: cycle detected ({include_name})",
                    control=Control.INCLUDE,
                ))
                continue
            if registry is None:
                prior_required_failed = True
                results.append(CheckResult(
                    name=include_name,
                    passed=False,
                    reason="include: no registry provided",
                    control=Control.INCLUDE,
                ))
                continue
            sub_chain = registry(include_name)
            if sub_chain is None:
                prior_required_failed = True
                results.append(CheckResult(
                    name=include_name,
                    passed=False,
                    reason="include: chain not registered",
                    control=Control.INCLUDE,
                ))
                continue
            sub_ctx = {**ctx, "_policy_chain_visited": frozenset(visited)}
            sub = evaluate_chain(
                include_name,
                sub_chain,
                tool_call,
                sub_ctx,
                registry=registry,
            )
            results.extend(sub.results)
            if not sub.passed:
                prior_required_failed = True
                results.append(CheckResult(
                    name=include_name,
                    passed=False,
                    reason=f"included chain failed: {sub.abort_reason or 'see results'}",
                    control=Control.INCLUDE,
                ))
            continue

        # Regular check.
        try:
            result = check(tool_call, ctx)
        except Exception as exc:  # noqa: BLE001
            result = CheckResult(
                name=getattr(check, "__name__", "<check>"),
                passed=False,
                reason=f"check raised: {type(exc).__name__}: {exc}",
                control=control,
            )
        result.control = control
        results.append(result)

        # REQUISITE — abort on failure.
        if not result.passed and control == Control.REQUISITE:
            prior_required_failed = True
            abort_reason = f"requisite failed: {result.name}: {result.reason}"
            break

        if not result.passed and control != Control.SUFFICIENT:
            prior_required_failed = True

        # SUFFICIENT — pass short-circuits the chain only if no prior required check failed.
        if result.passed and control == Control.SUFFICIENT and not prior_required_failed:
            break

    passed = (not abort_reason) and (not prior_required_failed) and all(
        r.passed for r in results if r.control != Control.SUFFICIENT
    )
    reasons = tuple(r.reason for r in results if r.reason)
    return ChainDecision(
        chain=chain_name,
        passed=passed,
        results=tuple(results),
        reasons=reasons,
        abort_reason=abort_reason,
    )


# --- Built-in checks -------------------------------------------------------

def tier_check(min_tier: str) -> Check:
    """Check that the call's tier is at least `min_tier`."""
    tier_rank = {"tier0": 0, "tier1": 1, "tier2": 2, "tier3": 3}
    min_rank = tier_rank.get(min_tier, 0)

    def _check(tool_call: dict[str, Any], _ctx: dict[str, Any]) -> CheckResult:
        actual = tool_call.get("tier", "tier0")
        actual_rank = tier_rank.get(actual, 0)
        passed = actual_rank >= min_rank
        return CheckResult(
            name=f"tier(min={min_tier})",
            passed=passed,
            reason="" if passed else f"call tier {actual} < required {min_tier}",
        )

    _check.__name__ = f"tier_{min_tier}"
    return _check


def path_allowlist_check(allow: list[str] | tuple[str, ...], block: list[str] | tuple[str, ...] = ()) -> Check:
    """Check that the call's path is in the allowlist and not in the blocklist."""
    from pathlib import Path
    allow_t = tuple(Path(a).resolve(strict=False) for a in allow)
    block_t = tuple(Path(b).resolve(strict=False) for b in block)

    def _check(tool_call: dict[str, Any], _ctx: dict[str, Any]) -> CheckResult:
        path = (tool_call.get("path") or tool_call.get("args", {}).get("path") or "").strip()
        if not path or "\x00" in path:
            return CheckResult(name="path_allowlist", passed=False, reason="no path in call")
        resolved = Path(path).resolve(strict=False)
        if any(resolved.is_relative_to(b) for b in block_t):
            return CheckResult(name="path_allowlist", passed=False, reason=f"path {path!r} in blocklist")
        if any(resolved.is_relative_to(a) for a in allow_t):
            return CheckResult(name="path_allowlist", passed=True)
        return CheckResult(name="path_allowlist", passed=False, reason=f"path {path!r} not in allowlist")

    _check.__name__ = "path_allowlist"
    return _check


def command_grep_check(deny: list[str] | tuple[str, ...]) -> Check:
    """Check that the call's command does not contain any denied substring."""
    deny_t = tuple(deny)

    def _check(tool_call: dict[str, Any], _ctx: dict[str, Any]) -> CheckResult:
        cmd = (tool_call.get("command") or tool_call.get("args", {}).get("command") or "").lower()
        for needle in deny_t:
            if needle.lower() in cmd:
                return CheckResult(
                    name="command_grep",
                    passed=False,
                    reason=f"command contains denied substring {needle!r}",
                )
        return CheckResult(name="command_grep", passed=True)

    _check.__name__ = "command_grep"
    return _check


__all__ = [
    "Chain",
    "ChainDecision",
    "ChainRegistry",
    "Check",
    "CheckResult",
    "Control",
    "command_grep_check",
    "evaluate_chain",
    "path_allowlist_check",
    "tier_check",
]
