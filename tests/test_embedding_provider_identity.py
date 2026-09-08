"""Embedding transport and persisted-vector identity regressions."""

from http.client import IncompleteRead
import json

import pytest

from algo_cli import embedding_binding, harness, main, tools
from algo_cli.config import Config

IDENTITY_A = "sha256:" + "a" * 64
IDENTITY_B = "sha256:" + "b" * 64


class GatewayResponse:
    status = 200

    def __init__(self, payload, headers=None):
        self.payload = json.dumps(payload).encode()
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, count):
        return self.payload[:count]


@pytest.mark.parametrize("reported_upstream", [None, "http://127.0.0.1:11434"])
def test_factory_never_accepts_vectors_from_an_unbound_or_different_gateway(monkeypatch, reported_upstream):
    selected = "http://127.0.0.1:22434"
    requests = []
    direct_hosts = []

    def open_gateway(request, **_kwargs):
        requests.append(request)
        headers = {"X-Algo-Ollama-Host": reported_upstream} if reported_upstream else {}
        return GatewayResponse({"embeddings": [[1.0, 0.0]]}, headers)

    class Client:
        def __init__(self, *, host, timeout, trust_env, follow_redirects):
            direct_hosts.append(host)
            assert timeout is None and not trust_env and not follow_redirects

        def embed(self, **_kwargs):
            return {"embeddings": [[0.0, 1.0]]}

    monkeypatch.setattr(main, "Client", Client)
    monkeypatch.setattr(tools, "gateway_ready", lambda: True)
    monkeypatch.setattr(tools, "_open_local_gateway", open_gateway)
    result = main.make_local_embed_fn(Config(host=selected), "fixture")(["public fixture"])
    assert result == [[0.0, 1.0]]
    assert direct_hosts == [selected]
    assert all(request.full_url.endswith("/supplemental/embed-bound/v1") for request in requests)
    assert all(request.get_header("X-algo-ollama-host") == selected for request in requests)


def test_factory_uses_a_gateway_bound_to_the_selected_endpoint(monkeypatch):
    selected = "http://127.0.0.1:22434"
    requests = []

    def open_gateway(request, **_kwargs):
        requests.append(request)
        return GatewayResponse({"embeddings": [[0.0, 1.0]]}, {"X-Algo-Ollama-Host": selected})

    monkeypatch.setattr(main, "Client", lambda **_kwargs: pytest.fail("qualified gateway should serve the request"))
    monkeypatch.setattr(tools, "gateway_ready", lambda: True)
    monkeypatch.setattr(tools, "_open_local_gateway", open_gateway)
    assert main.make_local_embed_fn(Config(host=selected), "fixture")(["public fixture"]) == [[0.0, 1.0]]
    assert requests[0].get_header("X-algo-ollama-host") == selected


def test_embed_tool_binds_the_scoped_ollama_endpoint(monkeypatch):
    selected = "http://127.0.0.1:22434"
    monkeypatch.setenv("OLLAMA_HOST", selected)
    requests = []

    def open_gateway(request, **_kwargs):
        requests.append(request)
        return GatewayResponse({"embeddings": [[1.0, 0.0]]}, {"X-Algo-Ollama-Host": selected})

    monkeypatch.setattr(tools, "_open_local_gateway", open_gateway)
    assert json.loads(tools.embed_text("public fixture", model="fixture"))["vector_count"] == 1
    assert requests[0].get_header("X-algo-ollama-host") == selected


@pytest.fixture
def public_identity_index(monkeypatch):
    monkeypatch.setattr(harness, "all_source_roots", lambda: ())
    records = [
        {
            "id": str(i),
            "harness": "fixture",
            "kind": "wiki",
            "title": f"Public note {i}",
            "path": f"note-{i}.md",
            "search_text": f"public note {i}",
        }
        for i in range(3)
    ]
    index = {"source_policy": harness._source_policy(), "record_count": 3, "records": records}
    harness.INDEX_PATH.write_text(json.dumps(index))
    harness._set_index_cache(None)
    return records


def _bound(identity, calls=None):
    def embed(texts):
        if calls is not None:
            calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    return embedding_binding.BoundEmbedding(embed, lambda: identity, identity)


def test_provider_change_rebuilds_equal_width_vectors_and_survives_restart(public_identity_index):
    assert harness.embed_index_records(_bound(IDENTITY_A), "fixture", dimensions=2)["ready"]
    assert harness.embedded_count("fixture", dimensions=2, embedding_identity=IDENTITY_B) == (0, 3)
    assert harness.embedding_progress("fixture", dimensions=2, embedding_identity=IDENTITY_B)["pending"] == 3
    assert not harness.stats(model="fixture", dimensions=2, embedding_identity=IDENTITY_B)["embeddings"]["complete"]
    calls = []
    result = harness.embed_index_records(_bound(IDENTITY_B, calls), "fixture", dimensions=2)
    assert result["ready"] and result["embedded"] == 3 and len(calls) == 1
    harness._set_index_cache(None)
    index = harness.load_index()
    assert all(row["embedding_identity"] == IDENTITY_B for row in index["records"])
    assert index["embeddings"]["requested_identity"] == IDENTITY_B
    assert harness.embedded_count("fixture", dimensions=2, embedding_identity=IDENTITY_B) == (3, 3)
    assert harness.embedding_summary(index)["requested_identity"] == IDENTITY_B


def test_same_model_name_and_width_do_not_reuse_another_provider_build(public_identity_index):
    assert harness.embed_index_records(_bound(IDENTITY_A), "fixture", dimensions=2)["ready"]
    calls = []
    result = harness.embed_index_records(_bound(IDENTITY_B, calls), "fixture", dimensions=2)
    assert result["ready"] and result["embedded"] == 3 and len(calls) == 1


def test_query_cache_does_not_cross_provider_identities(public_identity_index):
    harness._QUERY_VEC_CACHE.clear()
    calls = {IDENTITY_A: 0, IDENTITY_B: 0}
    functions, indexes = {}, {}
    for identity, vector in [(IDENTITY_A, [1.0, 0.0]), (IDENTITY_B, [0.0, 1.0])]:

        def embed(texts, identity=identity, vector=vector):
            calls[identity] += 1
            return [vector for _ in texts]

        functions[identity] = embedding_binding.BoundEmbedding(embed, lambda identity=identity: identity, identity)
        indexes[identity] = {
            "records": [
                {
                    **public_identity_index[0],
                    "embedding_model": "fixture",
                    "embedding_dimensions": 2,
                    "embedding_identity": identity,
                    "embedding": vector,
                }
            ]
        }
    for identity in (IDENTITY_A, IDENTITY_B, IDENTITY_A, IDENTITY_B):
        assert harness.retrieve_for_query(
            "public", functions[identity], "fixture", dimensions=2, index=indexes[identity]
        )
    assert calls == {IDENTITY_A: 1, IDENTITY_B: 1}


def test_changed_or_unavailable_provider_cannot_use_a_warm_query_cache(public_identity_index):
    current = [IDENTITY_A]
    fn = embedding_binding.BoundEmbedding(lambda texts: [[1.0, 0.0] for _ in texts], lambda: current[0], IDENTITY_A)
    assert harness.embed_index_records(fn, "fixture", dimensions=2)["ready"]
    assert harness.retrieve_for_query("public", fn, "fixture", dimensions=2)
    for value in (IDENTITY_B, None):
        current[0] = value
        assert harness.retrieve_for_query("public", fn, "fixture", dimensions=2) == []
        hits = harness.hybrid_search("public", fn, "fixture", dimensions=2)
        assert hits and all(row["rank_sources"] == ["keyword"] for row in hits)
        assert all(row["rank_provenance"]["embedding_coverage"] == 0 for row in hits)


def test_mid_batch_model_change_cannot_label_new_vectors_with_the_old_identity(public_identity_index):
    current = [IDENTITY_A]
    calls = []

    def embed(texts):
        calls.append(texts)
        if len(calls) == 2:
            current[0] = IDENTITY_B
        return [[1.0, 0.0] for _ in texts]

    fn = embedding_binding.BoundEmbedding(embed, lambda: current[0], IDENTITY_A)
    result = harness.embed_index_records(fn, "fixture", dimensions=2, batch_size=1)
    assert not result["ready"] and result["embedded"] == 1
    harness._set_index_cache(None)
    records = harness.load_index()["records"]
    assert sum(row.get("embedding_identity") == IDENTITY_A for row in records) == 1
    resumed = harness.embed_index_records(_bound(IDENTITY_B), "fixture", dimensions=2)
    assert resumed["ready"] and resumed["embedded"] == 3


def test_unknown_identity_cannot_make_an_index_ready(public_identity_index):
    fn = embedding_binding.BoundEmbedding(
        lambda _: pytest.fail("unverified embedding was requested"), lambda: None, None
    )
    result = harness.embed_index_records(fn, "fixture", dimensions=2)
    assert result["ready"] is False and result["embedded"] == 0
    assert result["reason"] == "embedding_identity_unavailable"


@pytest.mark.parametrize("bad", [None, True, "", "sha256:wrong", "unbound"])
def test_legacy_or_malformed_record_identity_is_not_qualified(public_identity_index, bad):
    row = {
        **public_identity_index[0],
        "embedding_model": "fixture",
        "embedding_dimensions": 2,
        "embedding_identity": bad,
        "embedding": [1.0, 0.0],
    }
    assert (
        harness.retrieve_for_query("public", _bound(IDENTITY_A), "fixture", dimensions=2, index={"records": [row]})
        == []
    )


def test_artifact_probe_binds_endpoint_and_digest_and_disables_proxies(monkeypatch):
    digest = ["a" * 64]
    observed = []

    class Opener:
        def open(self, request, timeout):
            observed.append((request.full_url, timeout))
            return GatewayResponse({"models": [{"name": "fixture:latest", "digest": digest[0]}]})

    def opener(proxy, redirect):
        assert proxy.proxies == {}
        assert redirect.redirect_request(None, None, 302, None, None, "http://example.com") is None
        return Opener()

    monkeypatch.setattr(embedding_binding, "build_opener", opener)
    first = embedding_binding.probe_ollama_identity("http://127.0.0.1:11434", "fixture")
    assert first and first == embedding_binding.probe_ollama_identity("http://127.0.0.1:11434/", "fixture")
    assert first != embedding_binding.probe_ollama_identity("http://127.0.0.1:22434", "fixture")
    digest[0] = "b" * 64
    assert first != embedding_binding.probe_ollama_identity("http://127.0.0.1:11434", "fixture")
    assert all(url.endswith("/api/tags") and timeout == 2.0 for url, timeout in observed)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"models": {}},
        {"models": []},
        {"models": [{"name": "fixture", "digest": True}]},
        {"models": [{"name": "fixture", "digest": "wrong"}]},
        {"models": [{"name": "other", "digest": "a" * 64}]},
        {"models": [{"name": "fixture", "model": "other", "digest": "a" * 64}]},
        {"models": [{"name": "fixture", "digest": "a" * 64}] * 2},
    ],
)
def test_untrusted_model_metadata_cannot_supply_an_identity(monkeypatch, payload):
    class Opener:
        def open(self, *_args, **_kwargs):
            return GatewayResponse(payload)

    monkeypatch.setattr(embedding_binding, "build_opener", lambda *_args: Opener())
    assert embedding_binding.probe_ollama_identity("http://127.0.0.1:11434", "fixture") is None


@pytest.mark.parametrize("command", ["automatic", "slash"])
def test_runtime_rebinds_same_name_after_provider_artifact_change(public_identity_index, monkeypatch, command):
    current = [IDENTITY_A]
    embedded = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def embed(self, **kwargs):
            embedded.append((current[0], len(kwargs["input"])))
            return {"embeddings": [[1.0, 0.0] for _ in kwargs["input"]]}

    monkeypatch.setattr(main, "Client", Client)
    monkeypatch.setattr(embedding_binding, "probe_ollama_identity", lambda *_args, **_kwargs: current[0])
    monkeypatch.setattr(tools, "gateway_ready", lambda: False)
    monkeypatch.setattr(main, "ollama_server_ready", lambda *_args: True)
    monkeypatch.setattr(main, "local_model_names", lambda *_args: ["fixture:latest"])
    monkeypatch.setattr(main, "show_info", lambda *_args: None)
    cfg = Config(harness_embed_model="fixture", embed_dimensions=2, embedding_backend="local")

    for identity in (IDENTITY_A, IDENTITY_B):
        current[0] = identity
        if command == "automatic":
            assert main.ensure_harness_index(cfg)
        else:
            from algo_cli.oliver_slash_dispatch import handle_command

            assert handle_command("/harness embed", cfg, None)[0]
        status = json.loads(tools.harness_stats(cfg=cfg))
        assert status["embeddings"]["requested_identity"] == identity
        assert status["embeddings"]["complete"]
    assert embedded == [(IDENTITY_A, 3), (IDENTITY_B, 3)]


@pytest.mark.parametrize("changed", [False, True])
def test_refresh_carries_provider_identity_only_for_reusable_sources(
    public_identity_index, monkeypatch, tmp_path, changed
):
    directory = tmp_path / "wiki"
    directory.mkdir()
    path = directory / "note.md"
    path.write_text("# Public note\nFirst source version.\n")
    monkeypatch.setattr(
        harness, "all_source_roots", lambda: (harness.SourceRoot("fixture", "wiki", directory, ("*.md",), 10),)
    )
    harness.load_index(refresh=True)
    assert harness.embed_index_records(_bound(IDENTITY_A), "fixture", dimensions=2)["ready"]
    if changed:
        path.write_text("# Public note\nChanged source version.\n")
    refreshed = harness.load_index(refresh=True)
    (row,) = refreshed["records"]
    assert (row.get("embedding_identity") == IDENTITY_A) is (not changed)


@pytest.mark.parametrize(
    "raw", [b'{"models":[],"models":[]}', b'{"models":NaN}', b"\xff", b"x" * (embedding_binding.MAX_TAGS_BYTES + 1)]
)
def test_metadata_parser_rejects_duplicates_nonfinite_and_oversized_input(monkeypatch, raw):
    class Opener:
        def open(self, *_args, **_kwargs):
            response = GatewayResponse({})
            response.payload = raw
            return response

    monkeypatch.setattr(embedding_binding, "build_opener", lambda *_args: Opener())
    assert embedding_binding.probe_ollama_identity("http://127.0.0.1:11434", "fixture") is None


def test_direct_local_embedding_disables_proxy_and_redirect_routing(monkeypatch):
    observed = []

    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        def embed(self, **_kwargs):
            return {"embeddings": [[1.0, 0.0]]}

    monkeypatch.setattr(main, "Client", Client)
    monkeypatch.setattr(tools, "gateway_ready", lambda: False)
    cfg = Config(host="http://127.0.0.1:22434")
    assert main.make_local_embed_fn(cfg, "fixture", timeout_seconds=3)(["public"])
    assert observed == [{"host": cfg.host, "timeout": 3, "trust_env": False, "follow_redirects": False}]


def test_hybrid_discards_vectors_when_the_provider_changes_after_vector_ranking(public_identity_index, monkeypatch):
    current = [IDENTITY_A]
    fn = embedding_binding.BoundEmbedding(lambda texts: [[1.0, 0.0] for _ in texts], lambda: current[0], IDENTITY_A)
    assert harness.embed_index_records(fn, "fixture", dimensions=2)["ready"]
    original = harness.retrieve_for_query

    def retrieve(*args, **kwargs):
        hits = original(*args, **kwargs)
        assert hits
        current[0] = IDENTITY_B
        return hits

    monkeypatch.setattr(harness, "retrieve_for_query", retrieve)
    hits = harness.hybrid_search("public", fn, "fixture", dimensions=2)
    assert hits and all(row["rank_sources"] == ["keyword"] for row in hits)
    assert all(row["rank_provenance"]["requested_identity"] is None for row in hits)


def test_embedding_readiness_rechecks_identity_after_the_last_progress_callback(public_identity_index):
    current = [IDENTITY_A]
    fn = embedding_binding.BoundEmbedding(lambda texts: [[1.0, 0.0] for _ in texts], lambda: current[0], IDENTITY_A)

    def progress(*_args):
        current[0] = IDENTITY_B

    result = harness.embed_index_records(fn, "fixture", dimensions=2, on_progress=progress)
    assert not result["ready"] and result["reason"] == "embedding_identity_changed"
    assert result["embedded"] == 3
    assert harness.embedded_count("fixture", dimensions=2, embedding_identity=IDENTITY_B) == (0, 3)


@pytest.mark.parametrize("query,kind", [("", None), ("public", "missing-kind")])
def test_empty_retrieval_does_not_probe_the_provider(public_identity_index, query, kind):
    assert harness.embed_index_records(_bound(IDENTITY_A), "fixture", dimensions=2)["ready"]
    fn = embedding_binding.BoundEmbedding(
        lambda _: pytest.fail("empty retrieval must not embed"),
        lambda: pytest.fail("empty retrieval must not probe metadata"),
        IDENTITY_A,
    )
    assert harness.hybrid_search(query, fn, "fixture", dimensions=2, kind=kind) == []


def test_turn_embedding_memo_checks_provider_before_returning_a_hit(monkeypatch):
    from test_main_helpers import _patch_agent_loop_for_tool_policy_test

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    current, calls = [IDENTITY_A], []

    def embed(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    fn = embedding_binding.BoundEmbedding(embed, lambda: current[0], IDENTITY_A)
    monkeypatch.setattr(main, "make_embed_fn", lambda *_a, **_kw: (fn, "local", "fixture"))
    monkeypatch.setattr(main, "json_sink", lambda: None)
    monkeypatch.setattr(main, "ensure_harness_index", lambda *_a: True)

    class FinishedProbe(Exception):
        pass

    def inspect_memo(_query, shared, *_args, **_kwargs):
        assert shared(["public"]) == shared(["public"]) == [[1.0, 0.0]]
        assert calls == [["public"]]
        current[0] = IDENTITY_B
        with pytest.raises(ValueError, match="embedding_identity_unavailable_or_changed"):
            shared(["public"])
        raise FinishedProbe

    monkeypatch.setattr(harness, "hybrid_search", inspect_memo)
    with pytest.raises(FinishedProbe):
        main.agent_loop(object(), Config(session_mode="explore", code_rag_enabled=False), "public")


@pytest.mark.parametrize(
    "host",
    [
        "https://example.com:443",
        "http://user@127.0.0.1:11434",
        "http://127.0.0.1:11434/path",
        "http://127.0.0.1:11434?x=1",
    ],
)
def test_metadata_probe_refuses_unqualified_endpoints(monkeypatch, host):
    monkeypatch.setattr(embedding_binding, "build_opener", lambda *_: pytest.fail("unqualified endpoint requested"))
    assert embedding_binding.probe_ollama_identity(host, "fixture") is None


def test_truncated_metadata_response_is_unavailable_not_an_uncaught_error(monkeypatch):
    class Opener:
        def open(self, *_args, **_kwargs):
            raise IncompleteRead(b"partial", 100)

    monkeypatch.setattr(embedding_binding, "build_opener", lambda *_args: Opener())
    assert embedding_binding.probe_ollama_identity("http://127.0.0.1:11434", "fixture") is None


@pytest.mark.parametrize(
    "filters", [{"kind": "missing-kind"}, {"harness_name": "missing-harness"}, {"empty_index": True}]
)
def test_public_search_empty_slice_does_not_probe_provider_metadata(public_identity_index, monkeypatch, filters):
    records = [] if filters.get("empty_index") else public_identity_index
    monkeypatch.setattr(harness, "retrieval_index", lambda **_kwargs: {"records": records})
    calls = []
    monkeypatch.setattr(
        embedding_binding, "probe_ollama_identity", lambda *_args, **_kwargs: calls.append(True) or IDENTITY_A
    )
    cfg = Config(echo_veil_enabled=False, harness_embed_model="fixture")
    result = tools.harness_search(
        "public", cfg=cfg, **{key: value for key, value in filters.items() if key != "empty_index"}
    )
    assert result.startswith("No harness matches.")
    assert calls == []
