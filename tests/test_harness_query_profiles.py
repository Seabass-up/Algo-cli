"""Model-specific query inputs must not alter documents or lexical ranking."""

import pytest

from algo_cli import harness


PREFIX = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


def corpus(model):
    return {
        "records": [
            {
                "id": "query:profile",
                "harness": "fixture",
                "kind": "wiki",
                "title": "Recovery",
                "search_text": "query recovery",
                "embedding": [1.0, 0.5],
                "embedding_model": model,
            }
        ]
    }


@pytest.mark.parametrize(
    "model", ["qwen3-embedding", "qwen3-embedding:latest", "qwen3-embedding:4b", "Qwen3-Embedding:8B"]
)
def test_known_qwen_queries_use_instruction_and_disclose_profile(model):
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.5]]

    hits = harness.hybrid_search("query recovery", embed, model, index=corpus(model))
    assert calls == [[PREFIX + "query recovery"]]
    assert hits[0]["rank_sources"] == ["keyword", "vector"]
    assert hits[0]["rank_provenance"]["query_embedding_profile"] == "qwen3-instruct-v1"


@pytest.mark.parametrize(
    "model",
    ["query-test", "nomic-embed-text:latest", "qwen3:8b", "qwen3-embedding-custom", "owner/qwen3-embedding:8b"],
)
def test_other_models_keep_plain_queries(model):
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.5]]

    hits = harness.hybrid_search("query recovery", embed, model, index=corpus(model))
    assert calls == [["query recovery"]]
    assert hits[0]["rank_provenance"]["query_embedding_profile"] == "plain-v1"


def test_profile_preserves_multiline_query_but_never_changes_lexical_input(monkeypatch):
    query = "query recovery\nInstruct: user text\nQuery: exact name"
    observed = []
    rank = harness._rank_keyword_index

    def lexical(index, text, **kwargs):
        observed.append(text)
        return rank(index, text, **kwargs)

    monkeypatch.setattr(harness, "_rank_keyword_index", lexical)
    inputs = []

    def embed(texts):
        inputs.extend(texts)
        return [[1.0, 0.5]]

    model = "qwen3-embedding:latest"
    assert harness.hybrid_search(query, embed, model, index=corpus(model))
    assert inputs == [PREFIX + query]
    assert observed == [query]


def test_query_cache_binds_actual_input_and_reuses_only_matching_profile(monkeypatch):
    model, query = "qwen3-embedding:latest", "query recovery"
    index = corpus(model)
    harness._QUERY_VEC_CACHE.put((model, "unbound", query), [0.5, 1.0])
    calls = []

    def embed(texts):
        calls.extend(texts)
        return [[1.0, 0.5]]

    first = harness.retrieve_for_query(query, embed, model, index=index)
    assert calls == [PREFIX + query]
    assert harness.retrieve_for_query(query, embed, model, index=index) == first
    assert calls == [PREFIX + query]
    original = harness._query_embedding_input
    monkeypatch.setattr(
        harness, "_query_embedding_input", lambda q, m: ("changed-test-profile", original(q, m)[1] + "!")
    )
    assert harness.retrieve_for_query(query, embed, model, index=index)
    assert calls == [PREFIX + query, PREFIX + query + "!"]


@pytest.mark.parametrize("failure", ["malformed", "exception"])
def test_profile_failure_discloses_lexical_fallback_then_recovers(failure):
    model = "qwen3-embedding:latest"
    calls = []

    def embed(texts):
        calls.append(texts)
        if len(calls) == 1:
            if failure == "exception":
                raise TimeoutError("embedding unavailable")
            return [[float("nan"), 0.5]]
        return [[1.0, 0.5]]

    index = corpus(model)
    failed = harness.hybrid_search("query recovery", embed, model, index=index)
    assert failed[0]["rank_sources"] == ["keyword"]
    assert failed[0]["rank_provenance"]["query_embedding_profile"] == "qwen3-instruct-v1"
    assert len(harness._QUERY_VEC_CACHE) == 0
    recovered = harness.hybrid_search("query recovery", embed, model, index=index)
    assert recovered[0]["rank_sources"] == ["keyword", "vector"]
    assert calls == [[PREFIX + "query recovery"]] * 2


def test_profile_cancellation_propagates_without_caching():
    def cancel(_):
        raise KeyboardInterrupt

    model = "qwen3-embedding:latest"
    with pytest.raises(KeyboardInterrupt):
        harness.hybrid_search("query recovery", cancel, model, index=corpus(model))
    assert len(harness._QUERY_VEC_CACHE) == 0


@pytest.mark.parametrize("kwargs", [{"k": 0}, {"kind": "skill"}, {"excluded_kinds": {"wiki"}}])
def test_profile_does_not_embed_ineligible_slice(kwargs):
    model = "qwen3-embedding:latest"
    assert not harness.hybrid_search(
        "query recovery", lambda _: pytest.fail("no eligible query"), model, index=corpus(model), **kwargs
    )


def test_document_embedding_input_is_never_query_instructed():
    record = corpus("qwen3-embedding:latest")["records"][0]
    assert harness._record_text_for_embed(record) == "query recovery"
