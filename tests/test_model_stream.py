"""Transport recovery remains bounded and cannot replay delivered calls."""

import httpx
from ollama import ResponseError
import pytest

from algo_cli import model_stream


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(model_stream.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ConnectionError("reset"),
                                    httpx.ReadError("reset"), ResponseError("busy", 503)])
def test_recover_before_usable_output_closes_each_attempt(failure):
    calls, closed = [], []

    def request():
        calls.append(1)
        attempt = len(calls)

        def stream():
            try:
                if attempt == 1:
                    yield {"message": {"thinking": "Planning"}}
                    raise failure
                yield {"message": {"content": "Observed answer"}}
            finally:
                closed.append(attempt)
        return stream()

    chunks = list(model_stream.recover_model_stream(request))
    assert len(calls) == 2 and closed == [1, 2]
    assert chunks[-1]["message"]["content"] == "Observed answer"
    assert len([chunk for chunk in chunks if "model_retry" in chunk]) == 1


@pytest.mark.parametrize("message", [{"content": "Partial"}, {"tool_calls": [{"id": "call-1"}]}])
def test_delivered_output_or_calls_prevent_retry(message):
    calls, closed = [], []

    def request():
        calls.append(1)

        def stream():
            try:
                yield {"message": message}
                raise TimeoutError("late timeout")
            finally:
                closed.append(1)
        return stream()

    with pytest.raises(TimeoutError):
        list(model_stream.recover_model_stream(request))
    assert calls == [1] and closed == [1]


@pytest.mark.parametrize("error", [ValueError("bad schema"), ResponseError("denied", 401),
                                  ResponseError("missing model", 404)])
def test_permanent_failures_are_not_retried(error):
    calls = []

    def request():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        list(model_stream.recover_model_stream(request))
    assert calls == [1]


def test_transport_retry_exhaustion_is_bounded():
    calls = []

    def request():
        calls.append(1)
        raise TimeoutError("offline")

    with pytest.raises(TimeoutError):
        list(model_stream.recover_model_stream(request))
    assert len(calls) == 3


def test_disabled_retries_keep_a_single_provider_request():
    calls = []

    def request():
        calls.append(1)
        return iter([{"message": {"thinking": "No answer"}}])

    chunks = list(model_stream.recover_model_stream(request, retries=False))
    assert calls == [1]
    assert not any("model_retry" in chunk for chunk in chunks)


def test_caller_close_cancels_retry_and_closes_provider():
    calls, closed = [], []

    def request():
        calls.append(1)

        def stream():
            try:
                yield {"message": {"thinking": "Still thinking"}}
            finally:
                closed.append(1)
        return stream()

    stream = model_stream.recover_model_stream(request)
    next(stream)
    stream.close()
    assert calls == [1] and closed == [1]
