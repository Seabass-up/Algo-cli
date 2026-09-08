"""Observed Ollama identity for public retrieval vectors, not attestation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import re
from typing import Callable
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .theodore_runtime_services import local_service_address

EmbedFn = Callable[[list[str]], list[list[float]]]
MAX_TAGS_BYTES = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate model metadata")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite model metadata")


def probe_ollama_identity(host: str, model: str, *, timeout: float = 2.0) -> str | None:
    """Return a bounded endpoint/model-artifact fingerprint, or unavailable."""
    if type(host) is not str or type(model) is not str or not model or len(model) > 256:
        return None
    endpoint = host.strip().rstrip("/")
    if local_service_address(endpoint) is None:
        return None
    try:
        request = Request(endpoint + "/api/tags", method="GET")
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(MAX_TAGS_BYTES + 1)
        if len(raw) > MAX_TAGS_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 4096:
            return None
        matches = []
        names = {model, model + ":latest"} if ":" not in model.rsplit("/", 1)[-1] else {model}
        for row in rows:
            if not isinstance(row, dict) or (row.get("name") or row.get("model")) not in names:
                continue
            if row.get("name") and row.get("model") and row["name"] != row["model"]:
                return None
            digest = row.get("digest")
            if type(digest) is not str or re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", digest) is None:
                return None
            matches.append(digest.removeprefix("sha256:").lower())
        if len(matches) != 1:
            return None
        descriptor = {
            "schema": "ollama-embedding-source-v1",
            "endpoint": endpoint,
            "model": model,
            "digest": matches[0],
        }
        return "sha256:" + hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    except (OSError, HTTPException, ValueError, TypeError, RecursionError):
        return None


@dataclass(frozen=True)
class BoundEmbedding:
    embed: EmbedFn
    probe: Callable[[], str | None]
    embedding_identity: str | None

    def validate_embedding_identity(self) -> bool:
        return self.embedding_identity is not None and self.probe() == self.embedding_identity

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.validate_embedding_identity():
            raise ValueError("embedding_identity_unavailable_or_changed")
        vectors = self.embed(texts)
        if not self.validate_embedding_identity():
            raise ValueError("embedding_identity_changed_during_request")
        return vectors


def bind_ollama_embedding(embed: EmbedFn, host: str, model: str) -> BoundEmbedding:
    def probe() -> str | None:
        return probe_ollama_identity(host, model)

    return BoundEmbedding(embed, probe, probe())
