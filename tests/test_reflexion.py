"""Focused correctness tests for Reflexion episode bookkeeping."""

from algo_cli.reasoning.reflexion import ReflexionEpisode, ReflexionLoop, run_reflexion_loop


def _episode(attempt: int, score: float) -> ReflexionEpisode:
    return ReflexionEpisode(
        attempt=attempt,
        task="solve the task",
        output=f"attempt {attempt}",
        critique=f"critique {attempt}",
        score=score,
        improved=False,
    )


def test_improved_compares_with_previous_attempt_not_all_time_best():
    loop = ReflexionLoop()

    for attempt, score in enumerate((0.8, 0.5, 0.7), start=1):
        loop.add_episode(_episode(attempt, score))

    assert [episode.improved for episode in loop.episodes] == [False, False, True]
    assert loop.best_score == 0.8
    assert loop.best_output == "attempt 1"


def test_zero_score_first_episode_is_still_the_best_available_output():
    loop = ReflexionLoop()

    loop.add_episode(_episode(1, 0.0))

    assert loop.best_score == 0.0
    assert loop.best_output == "attempt 1"


class _ScriptedClient:
    def __init__(self) -> None:
        self.responses = iter(
            (
                {"message": {"content": "first answer"}},
                {"message": {"content": '{"score": 0.4, "critique": "needs work"}'}},
                {"message": {"content": "revised answer"}},
                {"message": {"content": '{"score": 0.9, "critique": "complete"}'}},
            )
        )

    def chat(self, **_kwargs):
        return next(self.responses)


def test_run_reflexion_loop_constructs_episodes_and_tracks_improvement():
    episodes = run_reflexion_loop(
        task="produce a detailed implementation plan",
        client=_ScriptedClient(),
        model="test-model",
        max_attempts=2,
    )

    assert [episode.output for episode in episodes] == ["first answer", "revised answer"]
    assert [episode.score for episode in episodes] == [0.4, 0.9]
    assert [episode.improved for episode in episodes] == [False, True]
