"""Regression tests for browser-adjacent bug hunt defects (2026-11-09).

D1: cobalt_browser_service reported HTTP errors as "unreachable".
D2: url_scheme rejected upper-case scheme prefixes.
D3: url_scheme kept fragments, left path percent-encoding, and dropped blank query values.
D4: xenon_browser_egress treated an explicit port 0 as the scheme default.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from algo_cli import cobalt_browser_service, url_scheme
from algo_cli.xenon_browser_egress import (
    XenonEgressPolicy,
    XenonEgressRejected,
    resolve_public_url,
)

PUBLIC_V4 = "8.8.8.8"


def _resolver(*answers: str):
    def resolve(_host: str, _port: int):
        return answers

    return resolve


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _patch_opener(monkeypatch, fake_open) -> None:
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=fake_open),
    )


def _http_error(status: int, body: bytes, msg: str = "Error") -> HTTPError:
    return HTTPError("http://localhost:9377/tabs", status, msg, hdrs=None, fp=io.BytesIO(body))


# ---------------------------------------------------------------------------
# D1: HTTP status errors must not be reported as unreachable
# ---------------------------------------------------------------------------

def test_d1_request_http_404_is_not_unreachable(monkeypatch):
    def raise_404(req, timeout=None):
        raise _http_error(404, b'{"error": "no such tab"}', "Not Found")

    _patch_opener(monkeypatch, raise_404)
    result = cobalt_browser_service.list_tabs()
    assert result["code"] == "http_404"
    assert "404" in result["error"]
    assert "no such tab" in result["error"]
    assert "unreachable" not in result["error"]


def test_d1_request_http_500_without_json_body(monkeypatch):
    def raise_500(req, timeout=None):
        raise _http_error(500, b"<html>boom</html>", "Internal Server Error")

    _patch_opener(monkeypatch, raise_500)
    result = cobalt_browser_service.health()
    assert result["code"] == "http_500"
    assert "500" in result["error"]
    assert "unreachable" not in result["error"]


def test_d1_screenshot_http_404_is_not_unreachable(monkeypatch):
    def raise_404(req, timeout=None):
        raise _http_error(404, b'{"error": "no such tab"}', "Not Found")

    _patch_opener(monkeypatch, raise_404)
    result = cobalt_browser_service.screenshot("tab-123")
    assert result["code"] == "http_404"
    assert "no such tab" in result["error"]
    assert "ok" not in result


def test_d1_connection_failure_still_unreachable(monkeypatch):
    def raise_url_error(req, timeout=None):
        raise URLError("connection refused")

    _patch_opener(monkeypatch, raise_url_error)
    assert cobalt_browser_service.health()["code"] == "unreachable"
    assert cobalt_browser_service.screenshot("tab-123")["code"] == "unreachable"


# ---------------------------------------------------------------------------
# D2: URI scheme is case-insensitive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "ALGO-CLI://version",
        "Algo-Cli://version",
        "algo-cli:version",
        "ALGO-CLI:version",
    ],
)
def test_d2_scheme_prefix_is_case_insensitive(url: str):
    result = url_scheme.handle_deep_link(url)
    assert result["valid"] is True, result
    assert result["action"] == "version"


def test_d2_scheme_case_does_not_change_path_case():
    link = url_scheme.parse_deep_link("ALGO-CLI://skill/My-Skill")
    assert link is not None
    assert link.route == "skill"
    assert link.path == "My-Skill"


# ---------------------------------------------------------------------------
# D3: fragment, path decoding, blank query values
# ---------------------------------------------------------------------------

def test_d3a_fragment_is_removed_before_route_split():
    result = url_scheme.handle_deep_link("algo-cli://version#x")
    assert result["valid"] is True, result
    assert result["action"] == "version"


def test_d3a_fragment_removed_from_path_and_query():
    link = url_scheme.parse_deep_link("algo-cli://memory/recall?q=rebrand#top")
    assert link is not None
    assert link.route == "memory"
    assert link.path == "recall"
    assert link.query == {"q": "rebrand"}


def test_d3b_path_is_percent_decoded():
    link = url_scheme.parse_deep_link("algo-cli://skill/my%20skill")
    assert link is not None
    assert link.route == "skill"
    assert link.path == "my skill"


def test_d3b_encoded_slash_does_not_change_route_split():
    link = url_scheme.parse_deep_link("algo-cli://skill/a%2Fb")
    assert link is not None
    assert link.route == "skill"
    assert link.path == "a/b"


def test_d3c_blank_query_value_is_kept():
    link = url_scheme.parse_deep_link("algo-cli://session/load?name=")
    assert link is not None
    assert link.query == {"name": ""}


def test_d3_raw_url_is_preserved_exactly():
    url = "ALGO-CLI://skill/my%20skill?x=#frag"
    link = url_scheme.parse_deep_link(url)
    assert link is not None
    assert link.raw_url == url


# ---------------------------------------------------------------------------
# D4: explicit port 0 must not be treated as the scheme default
# ---------------------------------------------------------------------------

def test_d4_explicit_port_zero_is_denied_not_defaulted():
    with pytest.raises(XenonEgressRejected, match="port_denied"):
        resolve_public_url("https://example.com:0/", XenonEgressPolicy(), resolver=_resolver(PUBLIC_V4))


def test_d4_explicit_port_zero_denied_even_when_default_port_allowed():
    policy = XenonEgressPolicy(allowed_schemes=("http", "https"), allowed_ports=(80, 443))
    with pytest.raises(XenonEgressRejected, match="port_denied"):
        resolve_public_url("http://example.com:0/", policy, resolver=_resolver(PUBLIC_V4))


def test_d4_redirect_allowlist_port_zero_rejected():
    with pytest.raises(XenonEgressRejected, match="policy_redirect_allowlist"):
        XenonEgressPolicy(redirect_origin_allowlist=("https://example.com:0",))


def test_d4_default_port_still_used_when_port_absent():
    target = resolve_public_url("https://example.com/", XenonEgressPolicy(), resolver=_resolver(PUBLIC_V4))
    assert target.port == 443
