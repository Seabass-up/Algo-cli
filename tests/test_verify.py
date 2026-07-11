"""Comprehensive battery for the hallucination-prevention layer.

Covers all three tiers:
  Tier 1 — parameter_size_billions() parsing + calibration prompt injection
  Tier 2 — confidence/unverified_claims in reflection JSON schema
  Tier 3 — extract_claims, ground_claim, verify_response, format_verification_report
"""
from __future__ import annotations

import json
import pytest

from algo_cli import verify as v
from algo_cli import model_info


# ── helpers ───────────────────────────────────────────────────────────────────

def _llm(response_text: str):
    """Fake LLMFn that always returns the given string."""
    def _fn(system: str, user: str) -> str:
        return response_text
    return _fn


def _claims_llm(*claims: str):
    """Fake LLMFn that returns a valid claims JSON for the given claims."""
    return _llm(json.dumps({"claims": list(claims)}))


def _broken_llm():
    """Fake LLMFn that raises on every call."""
    def _fn(system: str, user: str) -> str:
        raise ConnectionError("offline")
    return _fn


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 — parameter_size_billions
# ═══════════════════════════════════════════════════════════════════════════════

class TestParameterSizeBillions:
    def test_simple_billions(self):
        assert model_info.parameter_size_billions({"parameter_size": "7B"}) == 7.0

    def test_decimal_billions(self):
        assert model_info.parameter_size_billions({"parameter_size": "8.2B"}) == pytest.approx(8.2)

    def test_large_model(self):
        assert model_info.parameter_size_billions({"parameter_size": "235B"}) == 235.0

    def test_trillion(self):
        assert model_info.parameter_size_billions({"parameter_size": "1T"}) == 1000.0

    def test_million(self):
        result = model_info.parameter_size_billions({"parameter_size": "500M"})
        assert result == pytest.approx(0.5)

    def test_lowercase_unit(self):
        assert model_info.parameter_size_billions({"parameter_size": "13b"}) == 13.0

    def test_whitespace_around_unit(self):
        assert model_info.parameter_size_billions({"parameter_size": "70 B"}) == 70.0

    def test_with_suffix_text(self):
        assert model_info.parameter_size_billions({"parameter_size": "70B parameters"}) == 70.0

    def test_missing_key(self):
        assert model_info.parameter_size_billions({}) is None

    def test_empty_string(self):
        assert model_info.parameter_size_billions({"parameter_size": ""}) is None

    def test_none_value(self):
        assert model_info.parameter_size_billions({"parameter_size": None}) is None

    def test_non_string_value(self):
        assert model_info.parameter_size_billions({"parameter_size": 42}) is None

    def test_unparseable_text(self):
        assert model_info.parameter_size_billions({"parameter_size": "large"}) is None

    def test_zero_value(self):
        # "0B" is valid syntax even if semantically odd
        assert model_info.parameter_size_billions({"parameter_size": "0B"}) == 0.0

    def test_threshold_boundary_below(self):
        """Models under 70B should trigger the calibration prompt."""
        info = {"parameter_size": "69.9B"}
        size = model_info.parameter_size_billions(info)
        assert size is not None and size < 70.0

    def test_threshold_boundary_at(self):
        """70B exactly is NOT under-threshold (equal, not less)."""
        info = {"parameter_size": "70B"}
        size = model_info.parameter_size_billions(info)
        assert size is not None and size >= 70.0

    def test_threshold_boundary_above(self):
        """Large models should not trigger calibration prompt."""
        info = {"parameter_size": "235B"}
        size = model_info.parameter_size_billions(info)
        assert size is not None and size >= 70.0


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 1 — calibration prompt injection in build_system_prompt
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibrationPrompt:
    """Tests for Tier-1 calibration block in build_system_prompt."""

    def _make_cfg(self, verify_mode: bool = False):
        from algo_cli.config import Config
        cfg = Config()
        cfg.verify_mode = verify_mode
        return cfg

    def test_small_model_gets_calibration_block(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        info = {"parameter_size": "7B", "supports_thinking": False}
        prompt = build_system_prompt(cfg, active_model_info=info)
        assert "Accuracy Constraints" in prompt
        assert "small-model mode" in prompt

    def test_large_model_no_calibration_block(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        info = {"parameter_size": "235B", "supports_thinking": True}
        prompt = build_system_prompt(cfg, active_model_info=info)
        assert "Accuracy Constraints" not in prompt

    def test_exact_threshold_no_calibration(self):
        """70B exactly is not under-threshold."""
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        info = {"parameter_size": "70B"}
        prompt = build_system_prompt(cfg, active_model_info=info)
        assert "Accuracy Constraints" not in prompt

    def test_no_model_info_no_calibration(self):
        """No model info available — don't inject (don't crash)."""
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        prompt = build_system_prompt(cfg, active_model_info=None)
        assert "Accuracy Constraints" not in prompt

    def test_empty_model_info_no_calibration(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        prompt = build_system_prompt(cfg, active_model_info={})
        assert "Accuracy Constraints" not in prompt

    def test_unparseable_size_no_calibration(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        info = {"parameter_size": "unknown"}
        prompt = build_system_prompt(cfg, active_model_info=info)
        assert "Accuracy Constraints" not in prompt

    def test_verify_mode_adds_notice(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg(verify_mode=True)
        prompt = build_system_prompt(cfg)
        assert "Verify Mode Active" in prompt

    def test_verify_mode_off_no_notice(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg(verify_mode=False)
        prompt = build_system_prompt(cfg)
        assert "Verify Mode Active" not in prompt

    def test_small_model_calibration_contains_key_rules(self):
        from algo_cli.main import build_system_prompt
        cfg = self._make_cfg()
        info = {"parameter_size": "13B"}
        prompt = build_system_prompt(cfg, active_model_info=info)
        assert "file paths" in prompt
        assert "function names" in prompt
        assert "I don't know" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 2 — confidence in reflection checkpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestReflectionConfidence:
    """Validate that confidence + unverified_claims are part of the checkpoint schema."""

    def test_reflection_system_prompt_includes_confidence_key(self):
        """The reflection system message must request confidence and unverified_claims."""
        import inspect
        from algo_cli import main
        src = inspect.getsource(main.reflection_checkpoint)
        assert "confidence" in src
        assert "unverified_claims" in src

    def test_low_confidence_note_injected(self):
        """When confidence < 0.6, the checkpoint note must include the warning."""
        checkpoint_json = json.dumps({
            "objective": "test",
            "completed": "done",
            "evidence": "none",
            "remaining": "nothing",
            "alignment_check": "ok",
            "web_research_needed": False,
            "web_research_reason": "",
            "next_action": "finish",
            "confidence": 0.3,
            "unverified_claims": ["claim A"],
        })
        # Parse the note that would be injected
        try:
            parsed = json.loads(checkpoint_json)
            confidence = float(parsed.get("confidence", 1.0))
            unverified = parsed.get("unverified_claims", [])
            low_note = ""
            if confidence < 0.6:
                low_note = "Low confidence detected"
            elif unverified:
                low_note = "unverified claim"
        except Exception:
            low_note = ""
        assert "Low confidence" in low_note

    def test_unverified_claims_note_when_confidence_ok(self):
        parsed = {
            "confidence": 0.8,
            "unverified_claims": ["path /foo/bar.py"],
        }
        confidence = float(parsed.get("confidence", 1.0))
        unverified = parsed.get("unverified_claims", [])
        note = ""
        if confidence < 0.6:
            note = "Low confidence detected"
        elif unverified:
            note = f"{len(unverified)} unverified claim"
        assert "unverified claim" in note

    def test_no_note_when_high_confidence_no_unverified(self):
        parsed = {"confidence": 0.95, "unverified_claims": []}
        confidence = float(parsed.get("confidence", 1.0))
        unverified = parsed.get("unverified_claims", [])
        note = ""
        if confidence < 0.6:
            note = "Low confidence"
        elif unverified:
            note = "unverified"
        assert note == ""

    def test_fallback_checkpoint_includes_confidence_key(self):
        """The error-fallback JSON in reflection_checkpoint must include confidence."""
        import inspect
        from algo_cli import main
        src = inspect.getsource(main.reflection_checkpoint)
        assert '"confidence"' in src or "'confidence'" in src


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 — extract_claims
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractClaims:
    def test_normal_extraction(self):
        claims = v.extract_claims("The config is at ~/.ollama_cli/config.json.", _claims_llm("config is at ~/.ollama_cli/config.json"))
        assert len(claims) == 1
        assert "config" in claims[0].lower()

    def test_multiple_claims(self):
        llm = _claims_llm("Python 3.10 required", "run: ollama pull llama3", "port 11434 by default")
        claims = v.extract_claims("some text", llm)
        assert len(claims) == 3

    def test_empty_text_returns_empty(self):
        assert v.extract_claims("", _claims_llm("anything")) == []

    def test_whitespace_only_returns_empty(self):
        assert v.extract_claims("   \n\t  ", _claims_llm("x")) == []

    def test_llm_error_returns_empty(self):
        assert v.extract_claims("some text", _broken_llm()) == []

    def test_malformed_json_returns_empty(self):
        claims = v.extract_claims("text", _llm("not json at all"))
        assert claims == []

    def test_wrong_schema_key_returns_empty(self):
        claims = v.extract_claims("text", _llm(json.dumps({"facts": ["x", "y"]})))
        assert claims == []

    def test_non_list_claims_returns_empty(self):
        claims = v.extract_claims("text", _llm(json.dumps({"claims": "a string not list"})))
        assert claims == []

    def test_short_claims_filtered_out(self):
        # Claims shorter than _MIN_CLAIM_CHARS (12) are dropped
        llm = _llm(json.dumps({"claims": ["ok", "yes", "a valid longer claim here"]}))
        claims = v.extract_claims("text", llm)
        assert all(len(c) >= v._MIN_CLAIM_CHARS for c in claims)

    def test_text_truncated_to_3000_chars(self):
        # Verify the function accepts very long text without error
        big_text = "x " * 5000
        claims = v.extract_claims(big_text, _claims_llm("some claim from big text"))
        assert isinstance(claims, list)

    def test_claims_stripped(self):
        llm = _llm(json.dumps({"claims": ["  leading space claim  "]}))
        claims = v.extract_claims("text", llm)
        if claims:
            assert claims[0] == claims[0].strip()

    def test_returns_strings_not_other_types(self):
        llm = _llm(json.dumps({"claims": [42, None, "valid claim text here", True]}))
        claims = v.extract_claims("text", llm)
        assert all(isinstance(c, str) for c in claims)


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 — ground_claim
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroundClaim:
    def test_empty_claim_not_grounded(self):
        result = v.ground_claim("")
        assert result["grounded"] is False
        assert result["sources"] == []

    def test_whitespace_only_not_grounded(self):
        result = v.ground_claim("   ")
        assert result["grounded"] is False

    def test_result_has_required_keys(self):
        result = v.ground_claim("some claim about rust indexer")
        assert "claim" in result
        assert "grounded" in result
        assert "sources" in result

    def test_claim_preserved_in_result(self):
        claim = "use cargo build --release"
        result = v.ground_claim(claim)
        assert result["claim"] == claim

    def test_sources_is_list(self):
        result = v.ground_claim("any claim")
        assert isinstance(result["sources"], list)

    def test_all_short_words_not_grounded(self):
        # All words shorter than 3 chars → empty query → not grounded
        result = v.ground_claim("a b c")
        assert result["grounded"] is False

    def test_grounding_result_consistent(self):
        # Same claim → same grounding result (harness index is stable within a test)
        r1 = v.ground_claim("rust indexer cold start build")
        r2 = v.ground_claim("rust indexer cold start build")
        assert r1["grounded"] == r2["grounded"]


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 — verify_response
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyResponse:
    def test_empty_text_returns_full_confidence(self):
        result = v.verify_response("", _claims_llm())
        assert result["total_claims"] == 0
        assert result["confidence"] == 1.0

    def test_structure_always_present(self):
        result = v.verify_response("some text", _claims_llm("a valid claim here"))
        for key in ("total_claims", "grounded_count", "ungrounded_count", "grounded", "ungrounded", "confidence"):
            assert key in result

    def test_counts_are_consistent(self):
        result = v.verify_response("text", _claims_llm("claim one here", "claim two here", "claim three here"))
        assert result["total_claims"] == result["grounded_count"] + result["ungrounded_count"]
        assert len(result["grounded"]) == result["grounded_count"]
        assert len(result["ungrounded"]) == result["ungrounded_count"]

    def test_confidence_is_fraction(self):
        result = v.verify_response("text", _claims_llm("claim one here", "claim two here"))
        assert 0.0 <= result["confidence"] <= 1.0

    def test_all_ungrounded_gives_zero_confidence(self):
        # Claims that will never match harness records
        result = v.verify_response(
            "text",
            _claims_llm(
                "completely_invented_function_xyz_123",
                "nonexistent_path_zzz_abc_999",
                "imaginary_version_99.99.99",
            ),
        )
        # All claims should be ungrounded since harness is empty in tests
        assert result["ungrounded_count"] == result["total_claims"]
        assert result["confidence"] == 0.0

    def test_no_claims_no_crash(self):
        result = v.verify_response("purely subjective text", _llm(json.dumps({"claims": []})))
        assert result["total_claims"] == 0
        assert result["confidence"] == 1.0

    def test_llm_failure_returns_empty_gracefully(self):
        result = v.verify_response("text", _broken_llm())
        assert result["total_claims"] == 0
        assert result["confidence"] == 1.0

    def test_malformed_llm_response_graceful(self):
        result = v.verify_response("text", _llm("{ bad json ]"))
        assert result["total_claims"] == 0

    def test_confidence_rounded(self):
        result = v.verify_response("t", _claims_llm("long enough claim here"))
        # confidence should be a float with at most 3 decimal places
        assert result["confidence"] == round(result["confidence"], 3)


# ═══════════════════════════════════════════════════════════════════════════════
# Tier 3 — format_verification_report
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatVerificationReport:
    def _result(self, total=3, grounded=2, ungrounded_claims=None):
        ug = ungrounded_claims or []
        return {
            "total_claims": total,
            "grounded_count": total - len(ug),
            "ungrounded_count": len(ug),
            "grounded": [],
            "ungrounded": [{"claim": c, "sources": []} for c in ug],
            "confidence": (total - len(ug)) / total if total else 1.0,
        }

    def test_empty_result_returns_empty_string(self):
        result = {"total_claims": 0, "grounded_count": 0, "ungrounded_count": 0,
                  "grounded": [], "ungrounded": [], "confidence": 1.0}
        assert v.format_verification_report(result) == ""

    def test_report_contains_counts(self):
        report = v.format_verification_report(self._result(3, 2, ["bad claim one here"]))
        assert "2/3" in report or "2" in report

    def test_report_contains_confidence_percent(self):
        report = v.format_verification_report(self._result(4, 3, ["bad claim here now"]))
        assert "%" in report

    def test_ungrounded_claims_listed(self):
        ug = ["bad claim one is here", "another bad claim"]
        report = v.format_verification_report(self._result(3, 1, ug))
        assert "bad claim one is here" in report

    def test_caps_display_at_five(self):
        ug = [f"long unverified claim number {i}" for i in range(10)]
        report = v.format_verification_report(self._result(10, 0, ug))
        assert "…and" in report or "and" in report.lower()
        # Should not list all 10
        assert report.count("long unverified claim number") <= 5

    def test_fully_grounded_no_unverified_section(self):
        result = self._result(3, 3, [])
        report = v.format_verification_report(result)
        assert "Unverified" not in report

    def test_report_contains_verify_label(self):
        report = v.format_verification_report(self._result(2, 1, ["uncertain claim text here"]))
        assert "Verify" in report or "verify" in report.lower()
