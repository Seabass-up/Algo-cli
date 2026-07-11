"""Tests for H21 — Context-Adaptive Parameter Selection."""
from __future__ import annotations

from algo_cli.intelligence.context_adaptive import ContextAdaptiveSelector


def test_register_pattern() -> None:
    sel = ContextAdaptiveSelector()
    sel.register_pattern("aggressive", ["attack", "exploit"], {"temperature": 0.9})
    assert sel.count() == 1


def test_classify_matches_pattern() -> None:
    sel = ContextAdaptiveSelector()
    sel.set_defaults({"temperature": 0.5})
    sel.register_pattern("aggressive", ["attack", "exploit"], {"temperature": 0.9})
    result = sel.classify("Let's attack the target")
    assert "aggressive" in result.matched_patterns
    assert result.selected_parameters["temperature"] == 0.9


def test_classify_no_match_uses_defaults() -> None:
    sel = ContextAdaptiveSelector()
    sel.set_defaults({"temperature": 0.5})
    sel.register_pattern("aggressive", ["attack"], {"temperature": 0.9})
    result = sel.classify("Hello world")
    assert result.matched_patterns == []
    assert result.selected_parameters["temperature"] == 0.5


def test_classify_with_history() -> None:
    sel = ContextAdaptiveSelector()
    sel.set_defaults({"temperature": 0.5})
    sel.register_pattern("aggressive", ["attack"], {"temperature": 0.9})
    result = sel.classify("Hello", history=["attack the server", "exploit now"])
    assert "aggressive" in result.matched_patterns


def test_confidence_increases_with_more_matches() -> None:
    sel = ContextAdaptiveSelector()
    sel.register_pattern("p1", ["attack"], {"temp": 0.9})
    sel.register_pattern("p2", ["exploit"], {"temp": 0.8})
    r1 = sel.classify("attack")
    r2 = sel.classify("attack and exploit")
    assert r2.confidence > r1.confidence


def test_to_dict() -> None:
    sel = ContextAdaptiveSelector()
    sel.set_defaults({"temp": 0.5})
    sel.register_pattern("p1", ["attack"], {"temp": 0.9})
    result = sel.classify("attack")
    d = result.to_dict()
    assert "matched_patterns" in d
    assert "confidence" in d
    assert "selected_parameters" in d


def test_get_patterns() -> None:
    sel = ContextAdaptiveSelector()
    sel.register_pattern("p1", ["a"], {})
    sel.register_pattern("p2", ["b"], {})
    assert len(sel.get_patterns()) == 2