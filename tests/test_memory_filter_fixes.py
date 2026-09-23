from __future__ import annotations

import pytest

from algo_cli import julia_memory_candidates as memory_candidates

# Standing-rule sentences that report the past or complain are not directives.
NON_DIRECTIVE = [
    "You always forget to run the tests.",
    "You never read the file before editing it.",
    "You always skip the linter before pushing.",
    "You never ignore my formatting preferences.",
    "I always forget to run the tests.",
    "We always forget to update the changelog.",
    "I always miss the release checklist.",
    "I never said to delete that branch.",
    "I always used vim at my old job.",
    "You always deleted the wrong branch.",
    "We never tested the release on Windows.",
    "I always mess up the release notes.",
    "You always exceeded the token budget.",
    "You always seeded the wrong database.",
    "We never succeeded in reproducing it.",
]

# Genuine standing rules, including base and past forms of "-ed" verbs.
DIRECTIVE = [
    "I always use tabs for indentation.",
    "We never push to main.",
    "You always read CLAUDE.md before editing.",
    "Always run the tests.",
    "You always needed a backup before editing",
    "You always embedded the receipt in the note",
    "We always need two reviewers on releases.",
    "You always embed the receipt in the note.",
    "We always proceed with a dry run first.",
    "You always seed the database before integration tests.",
    "We never skip code review on releases.",
    "We never break the public API without a major bump.",
    "We never fail silently on configuration errors.",
    "You should always forget cached tokens after logout.",
    # "we/I always <verb> X" with a non-lapse verb is a team norm or preference.
    "We always fail fast on invalid configuration.",
    "We always ignore generated files when linting.",
    "We always skip the slow integration tests in pre-commit.",
    "We always break lines at 120 characters.",
    "We always break ties by timestamp.",
    "I always skip the intro section when summarizing.",
    # "we/I never <verb> X" is a standing rule, not a boast.
    "We never ignore failing tests.",
    "I never ignore compiler warnings.",
    "We never miss a security patch release.",
    "We never overlook accessibility checks.",
]


@pytest.mark.parametrize("text", NON_DIRECTIVE)
def test_non_directive_standing_sentences_are_rejected(text: str) -> None:
    candidates = memory_candidates.extract_candidates(text)
    assert [candidate.marker for candidate in candidates] == ["standing_rule"]
    decision = memory_candidates.evaluate_candidate(candidates[0])
    assert (decision.eligible, decision.reason) == (False, "not_directive")


@pytest.mark.parametrize("text", DIRECTIVE)
def test_directive_standing_rules_are_eligible(text: str) -> None:
    candidates = memory_candidates.extract_candidates(text)
    assert [candidate.marker for candidate in candidates] == ["standing_rule"]
    decision = memory_candidates.evaluate_candidate(candidates[0])
    assert (decision.eligible, decision.reason) == (True, "eligible")


@pytest.mark.parametrize(
    "text",
    ["You always needed a backup before editing", "You always embedded the receipt in the note"],
)
def test_ed_verb_exceptions_match_inflected_forms(text: str) -> None:
    assert memory_candidates._NON_DIRECTIVE_STANDING_RE.match(text) is None


def test_first_person_lapse_matches_second_person_lapse() -> None:
    first = memory_candidates.extract_candidates("I always forget to run the tests")[0]
    second = memory_candidates.extract_candidates("You always forget to run the tests")[0]
    assert memory_candidates.evaluate_candidate(first).reason == "not_directive"
    assert memory_candidates.evaluate_candidate(second).reason == "not_directive"
