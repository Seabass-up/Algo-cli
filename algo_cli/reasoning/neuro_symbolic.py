"""Neuro-Symbolic Verifier Loop.

Implements a guess-and-check pattern:
1. LLM proposes a solution (guess)
2. Symbolic/structured verifier checks correctness (check)
3. If verification fails, feed back the error and retry

Verifiers include:
- Python AST/expression evaluation for math/logic
- JSON schema validation for structured outputs
- Regex/grammar checks for format compliance
- Unit-test-like assertions for code

This dramatically reduces hallucination for formal domains.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..chat_protocol import get_attr


@dataclass
class VerificationResult:
    """Result of verifying an LLM output."""
    passed: bool
    checks_total: int
    checks_passed: int
    failures: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if self.checks_total == 0:
            return 0.0
        return self.checks_passed / self.checks_total

    def feedback_message(self) -> str:
        if self.passed:
            return "All verification checks passed."
        lines = [f"Verification failed: {self.checks_passed}/{self.checks_total} checks passed."]
        for fail in self.failures:
            lines.append(f"  - {fail}")
        if self.suggestions:
            lines.append("Suggestions:")
            for s in self.suggestions:
                lines.append(f"  + {s}")
        return "\n".join(lines)


@dataclass
class NeuroSymbolicVerifier:
    """LLM propose + symbolic verify loop."""
    max_rounds: int = 3
    verifiers: list[Callable[[str], VerificationResult]] = field(default_factory=list)

    def verify(self, output: str) -> VerificationResult:
        """Run all registered verifiers against the LLM output."""
        if not self.verifiers:
            return VerificationResult(passed=True, checks_total=0, checks_passed=0)
        total = 0
        passed = 0
        failures: list[str] = []
        suggestions: list[str] = []
        for verifier in self.verifiers:
            result = verifier(output)
            total += result.checks_total
            passed += result.checks_passed
            failures.extend(result.failures)
            suggestions.extend(result.suggestions)
        return VerificationResult(
            passed=passed == total and total > 0,
            checks_total=total,
            checks_passed=passed,
            failures=failures,
            suggestions=suggestions,
        )

    def add_verifier(self, verifier: Callable[[str], VerificationResult]) -> "NeuroSymbolicVerifier":
        self.verifiers.append(verifier)
        return self


# --- Built-in verifiers ---

def json_schema_verifier(schema: dict[str, Any]) -> Callable[[str], VerificationResult]:
    """Create a verifier that checks if output is valid JSON matching a schema."""
    def verify(output: str) -> VerificationResult:
        failures: list[str] = []
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            return VerificationResult(
                passed=False, checks_total=1, checks_passed=0,
                failures=[f"Invalid JSON: {e}"],
                suggestions=["Ensure output is valid JSON."],
            )
        # Check required keys
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                failures.append(f"Missing required key: {key}")
        # Check types
        properties = schema.get("properties", {})
        for key, expected_type in properties.items():
            if key in data:
                actual_type = type(data[key]).__name__
                if expected_type != actual_type:
                    failures.append(f"Key '{key}' has type {actual_type}, expected {expected_type}")
        passed = len(failures) == 0
        return VerificationResult(
            passed=passed,
            checks_total=len(required) + len(properties),
            checks_passed=len(required) + len(properties) - len(failures),
            failures=failures,
            suggestions=["Fix the schema violations listed above."],
        )
    return verify


def python_syntax_verifier(output: str) -> VerificationResult:
    """Verify that Python code in the output is syntactically valid."""
    # Extract code blocks
    code_blocks = re.findall(r"```python\n(.*?)```", output, re.DOTALL)
    if not code_blocks:
        # Check if entire output looks like code
        if any(kw in output for kw in ["def ", "class ", "import "]):
            code_blocks = [output]
        else:
            return VerificationResult(passed=True, checks_total=0, checks_passed=0)

    failures: list[str] = []
    for i, code in enumerate(code_blocks):
        try:
            ast.parse(code)
        except SyntaxError as e:
            failures.append(f"Code block {i+1}: SyntaxError at line {e.lineno}: {e.msg}")

    return VerificationResult(
        passed=len(failures) == 0,
        checks_total=len(code_blocks),
        checks_passed=len(code_blocks) - len(failures),
        failures=failures,
        suggestions=["Fix syntax errors in the code blocks."],
    )


def regex_format_verifier(pattern: str, description: str = "") -> Callable[[str], VerificationResult]:
    """Create a verifier that checks if output matches a regex pattern."""
    desc = description or f"matches pattern: {pattern[:50]}"
    def verify(output: str) -> VerificationResult:
        if re.search(pattern, output):
            return VerificationResult(passed=True, checks_total=1, checks_passed=1)
        return VerificationResult(
            passed=False, checks_total=1, checks_passed=0,
            failures=[f"Output does not {desc}"],
            suggestions=[f"Ensure the output {desc}"],
        )
    return verify


def assertion_verifier(assertions: list[tuple[str, str]]) -> Callable[[str], VerificationResult]:
    """Create a verifier from a list of (pattern, description) assertions.

    Each assertion is a (regex_pattern, description) tuple.
    The output must match ALL patterns.
    """
    def verify(output: str) -> VerificationResult:
        failures: list[str] = []
        for pattern, desc in assertions:
            if not re.search(pattern, output):
                failures.append(f"Assertion failed: {desc}")
        return VerificationResult(
            passed=len(failures) == 0,
            checks_total=len(assertions),
            checks_passed=len(assertions) - len(failures),
            failures=failures,
        )
    return verify


VERIFY_PROMPT_TEMPLATE = """You previously produced this output:

{output}

Verification found these issues:
{feedback}

Please revise your output to address all verification failures.
Produce the corrected output only, without explaining the changes."""


def run_neuro_symbolic(
    *,
    task: str,
    client: Any,
    model: str,
    verifiers: list[Callable[[str], VerificationResult]],
    max_rounds: int = 3,
    system: str = "You are a precise reasoning agent. Produce output that passes all verification checks.",
) -> tuple[str, list[VerificationResult]]:
    """Run a neuro-symbolic guess-and-check loop.

    Args:
        task: The task to solve.
        client: Ollama client.
        model: Model name.
        verifiers: List of verification functions.
        max_rounds: Maximum guess-and-check rounds.
        system: System prompt.

    Returns:
        (final_output, verification_history)
    """
    ns = NeuroSymbolicVerifier(max_rounds=max_rounds, verifiers=verifiers)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    history: list[VerificationResult] = []
    output = ""

    for round_num in range(max_rounds):
        try:
            response = client.chat(model=model, messages=messages, stream=False)
            output = get_attr(get_attr(response, "message", {}), "content", "").strip()
        except Exception as exc:
            output = f"Error: {exc}"

        # Verify
        result = ns.verify(output)
        history.append(result)

        if result.passed:
            break

        # Feed back failures for retry
        feedback = result.feedback_message()
        messages.append({"role": "assistant", "content": output})
        messages.append({
            "role": "user",
            "content": VERIFY_PROMPT_TEMPLATE.format(output=output[:2000], feedback=feedback),
        })

    return output, history
