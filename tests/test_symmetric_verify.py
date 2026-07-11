"""Tests for H14 — Symmetric Encode/Decode Verification."""
from __future__ import annotations

from algo_cli.intelligence.symmetric_verify import SymmetricVerifier


def test_register_pair() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v", "Creator", "Verifier")
    assert sv.count() == 1


def test_check_symmetry_all_present() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v")
    sv.register("H2", "H2v")
    missing = sv.check_symmetry({"H1", "H1v", "H2", "H2v"})
    assert missing == []


def test_check_symmetry_missing_verifier() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v")
    sv.register("H2", "H2v")
    missing = sv.check_symmetry({"H1", "H2"})
    assert "H1" in missing
    assert "H2" in missing


def test_check_symmetry_missing_creator_not_flagged() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v")
    # Only verifier present, creator missing — not flagged
    missing = sv.check_symmetry({"H1v"})
    assert missing == []


def test_find_pair() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v", "Creator", "Verifier")
    pair = sv.find_pair("H1")
    assert pair is not None
    assert pair.verifier_id == "H1v"


def test_find_pair_missing() -> None:
    sv = SymmetricVerifier()
    assert sv.find_pair("nope") is None


def test_get_pairs() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v")
    sv.register("H2", "H2v")
    assert len(sv.get_pairs()) == 2


def test_to_dict() -> None:
    sv = SymmetricVerifier()
    sv.register("H1", "H1v", "Creator", "Verifier")
    d = sv.get_pairs()[0].to_dict()
    assert d["creator_id"] == "H1"
    assert d["verifier_title"] == "Verifier"