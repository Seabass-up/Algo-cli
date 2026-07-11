"""Hallucination-prevention layer for small models.

Three components wired together:
  1. parameter_size_billions()  — parse model size string to float (lives in model_info)
  2. extract_claims()           — maintenance-model pass decomposing a response into
                                  individual verifiable factual claims
  3. ground_claim()             — each claim is checked against the harness index;
                                  ungrounded claims are returned for user visibility
  4. verify_response()          — orchestrates 2+3 and returns a structured report
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from . import harness

LLMFn = Callable[[str, str], str]

_MIN_CLAIM_CHARS = 12

_EXTRACT_SYSTEM = (
    "You are a factual claim extractor. Given text, list every verifiable factual claim "
    "as a JSON array under the key 'claims'. A claim is a specific, checkable assertion: "
    "a file path, function name, version number, shell command, URL, configuration key, "
    "or named technical concept. Omit vague statements and subjective opinions. "
    'Output only valid JSON: {"claims": ["claim1", "claim2", ...]}'
)

_WORD_RE = re.compile(r"[a-zA-Z0-9_\-\.]+")
_COMMON_WORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "file", "uses",
        "use", "has", "have", "will", "into", "json", "text", "data", "code",
        "line", "lines", "path",
    }
)


def extract_claims(text: str, llm_fn: LLMFn) -> list[str]:
    """Use a maintenance LLM pass to extract factual claims from model output.

    Returns an empty list on any failure — never raises.
    """
    if not text or not text.strip():
        return []
    try:
        raw = llm_fn(_EXTRACT_SYSTEM, text)
        data = json.loads(raw)
        claims = data.get("claims", [])
        if not isinstance(claims, list):
            return []
        return [
            str(c).strip()
            for c in claims
            if str(c).strip() and len(str(c).strip()) >= _MIN_CLAIM_CHARS
        ]
    except Exception:
        return []


def ground_claim(claim: str, *, limit: int = 3) -> dict[str, Any]:
    """Check whether a claim has supporting evidence in the harness index.

    Returns:
        {"claim": str, "grounded": bool, "sources": list[str]}
    """
    if not claim or not claim.strip():
        return {"claim": claim, "grounded": False, "sources": []}
    words = _WORD_RE.findall(claim)
    keywords = [w for w in words if len(w) >= 4 and w.lower() not in _COMMON_WORDS][:8]
    query = " ".join(keywords)
    if not query:
        return {"claim": claim, "grounded": False, "sources": []}
    try:
        results = harness.search_index(query, limit=limit)
    except Exception:
        results = []
    claim_tokens = {w.lower() for w in keywords}
    sources: list[str] = []
    for result in results:
        haystack = " ".join(
            str(result.get(key, ""))
            for key in ("id", "title", "path", "relative_path", "description", "summary", "search_text")
        ).lower()
        if any(token in haystack for token in claim_tokens):
            if result.get("id"):
                sources.append(str(result["id"]))
    if sources:
        return {
            "claim": claim,
            "grounded": True,
            "sources": sources[:limit],
        }
    return {"claim": claim, "grounded": False, "sources": []}


def verify_response(text: str, llm_fn: LLMFn) -> dict[str, Any]:
    """Full claim-extraction + harness-grounding pipeline.

    Returns:
        {
            "total_claims": int,
            "grounded_count": int,
            "ungrounded_count": int,
            "grounded": [...],
            "ungrounded": [...],
            "confidence": float,   # fraction of claims grounded (1.0 if no claims)
        }
    """
    claims = extract_claims(text, llm_fn)
    if not claims:
        return {
            "total_claims": 0,
            "grounded_count": 0,
            "ungrounded_count": 0,
            "grounded": [],
            "ungrounded": [],
            "confidence": 1.0,
        }
    results = [ground_claim(c) for c in claims]
    grounded = [r for r in results if r["grounded"]]
    ungrounded = [r for r in results if not r["grounded"]]
    confidence = len(grounded) / len(results) if results else 1.0
    return {
        "total_claims": len(claims),
        "grounded_count": len(grounded),
        "ungrounded_count": len(ungrounded),
        "grounded": grounded,
        "ungrounded": ungrounded,
        "confidence": round(confidence, 3),
    }


def format_verification_report(result: dict[str, Any]) -> str:
    """Human-readable summary of a verify_response result."""
    total = result.get("total_claims", 0)
    if total == 0:
        return ""
    grounded_count = result["grounded_count"]
    confidence = result["confidence"]
    lines = [
        f"[Verify] {grounded_count}/{total} claims grounded in harness  "
        f"(confidence {confidence:.0%})"
    ]
    ungrounded = result.get("ungrounded", [])
    if ungrounded:
        lines.append("Unverified claims (not found in harness index):")
        for item in ungrounded[:5]:
            lines.append(f"  · {item['claim']}")
        if len(ungrounded) > 5:
            lines.append(f"  · …and {len(ungrounded) - 5} more")
    return "\n".join(lines)
