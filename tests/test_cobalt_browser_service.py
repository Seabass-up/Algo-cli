"""Tests for the Cobalt browser service client and browser tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import URLError


from algo_cli import cobalt_browser_service, tools


# ---------------------------------------------------------------------------
# cobalt_browser_service client module
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal mock for urlopen return value."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _fake_json_response(body: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(body).encode("utf-8"))


def _fake_binary_response(data: bytes) -> _FakeResponse:
    return _FakeResponse(data)


def test_health_returns_ok(monkeypatch):
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response({"ok": True, "running": True})),
    )
    result = cobalt_browser_service.health()
    assert result["ok"] is True
    assert result["running"] is True


def test_is_available_true_when_healthy(monkeypatch):
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response({"ok": True, "running": True})),
    )
    assert cobalt_browser_service.is_available() is True


def test_is_available_false_when_unreachable(monkeypatch):
    def raise_url_error(req, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=raise_url_error),
    )
    assert cobalt_browser_service.is_available() is False


def test_open_tab_returns_tab_id(monkeypatch):
    captured = {}

    def fake_open(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.method
        return _fake_json_response({"tabId": "abc-123", "url": "https://example.com/"})

    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=fake_open),
    )
    result = cobalt_browser_service.open_tab("https://example.com")
    assert result["tabId"] == "abc-123"
    assert result["url"] == "https://example.com/"
    body = json.loads(captured["data"])
    assert body["url"] == "https://example.com"
    assert body["userId"] == "algo-cli"


def test_open_tab_rejects_empty_url():
    result = cobalt_browser_service.open_tab("")
    assert "error" in result
    assert result["code"] == "invalid_url"


def test_snapshot_returns_accessibility_tree(monkeypatch):
    snapshot_body = {
        "url": "https://example.com/",
        "snapshot": '- heading "Example Domain" [level=1]\n- link "More" [e1]',
        "refsCount": 1,
        "truncated": False,
        "totalChars": 42,
    }
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response(snapshot_body)),
    )
    result = cobalt_browser_service.snapshot("tab-123")
    assert result["snapshot"] == snapshot_body["snapshot"]
    assert result["refsCount"] == 1


def test_click_by_ref(monkeypatch):
    captured = {}

    def fake_open(req, timeout=None):
        captured["data"] = json.loads(req.data)
        captured["method"] = req.method
        return _fake_json_response({"ok": True, "url": "https://example.com/page2", "refsAvailable": True})

    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=fake_open),
    )
    result = cobalt_browser_service.click("tab-1", ref="e1")
    assert result["ok"] is True
    assert captured["data"]["ref"] == "e1"
    assert captured["method"] == "POST"


def test_click_requires_target():
    result = cobalt_browser_service.click("tab-1")
    assert result["code"] == "invalid_target"


def test_type_text(monkeypatch):
    captured = {}

    def fake_open(req, timeout=None):
        captured["data"] = json.loads(req.data)
        return _fake_json_response({"ok": True})

    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=fake_open),
    )
    result = cobalt_browser_service.type_text("tab-1", "hello world", ref="e1")
    assert result["ok"] is True
    assert captured["data"]["text"] == "hello world"
    assert captured["data"]["ref"] == "e1"


def test_scroll_validates_direction():
    result = cobalt_browser_service.scroll("tab-1", direction="sideways")
    assert result["code"] == "invalid_direction"


def test_navigate(monkeypatch):
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response(
            {"ok": True, "tabId": "tab-1", "url": "https://example.org/", "refsAvailable": True}
        )),
    )
    result = cobalt_browser_service.navigate("tab-1", "https://example.org")
    assert result["ok"] is True
    assert result["url"] == "https://example.org/"


def test_close_tab(monkeypatch):
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response({"ok": True})),
    )
    result = cobalt_browser_service.close_tab("tab-1")
    assert result["ok"] is True


def test_screenshot_returns_binary(monkeypatch):
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_binary_response(png_data)),
    )
    result = cobalt_browser_service.screenshot("tab-1")
    assert result["ok"] is True
    assert result["data"] == png_data
    assert result["size"] == len(png_data)


def test_evaluate(monkeypatch):
    monkeypatch.setattr(
        cobalt_browser_service,
        "build_opener",
        lambda: SimpleNamespace(open=lambda req, timeout=None: _fake_json_response(
            {"ok": True, "result": "Example Domain"}
        )),
    )
    result = cobalt_browser_service.evaluate("tab-1", "document.title")
    assert result["ok"] is True
    assert result["result"] == "Example Domain"


# ---------------------------------------------------------------------------
# tools.py browser tool functions
# ---------------------------------------------------------------------------

def test_cobalt_open_returns_unavailable_when_service_down(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: False)
    out = tools.cobalt_open("https://example.com")
    assert "not available" in out


def test_cobalt_open_returns_tab_id(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "open_tab",
        lambda url: {"tabId": "xyz-789", "url": "https://example.com/"},
    )
    out = tools.cobalt_open("https://example.com")
    assert "xyz-789" in out
    assert "https://example.com/" in out


def test_cobalt_open_reports_error(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "open_tab",
        lambda url: {"error": "connection refused", "code": "unreachable"},
    )
    out = tools.cobalt_open("https://example.com")
    assert "connection refused" in out


def test_cobalt_snapshot_returns_text(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "snapshot",
        lambda tab_id, user_id="algo-cli": {
            "url": "https://example.com/",
            "snapshot": '- heading "Example Domain" [level=1]',
            "refsCount": 5,
            "truncated": False,
        },
    )
    out = tools.cobalt_snapshot("tab-1")
    assert "Example Domain" in out
    assert "Refs: 5" in out


def test_cobalt_click_reports_retryable_error(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "click",
        lambda tab_id, ref=None, selector=None, user_id="algo-cli": {
            "error": "Page changed during click",
            "retryable": True,
            "code": "page_changed",
        },
    )
    out = tools.cobalt_click("tab-1", ref="e1")
    assert "Page changed" in out
    assert "cobalt_snapshot" in out


def test_cobalt_type_returns_success(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "type_text",
        lambda tab_id, text, ref=None, selector=None, user_id="algo-cli": {"ok": True},
    )
    out = tools.cobalt_type("tab-1", "hello", ref="e1")
    assert "5 characters" in out


def test_cobalt_scroll_returns_success(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "scroll",
        lambda tab_id, direction="down", amount=3, user_id="algo-cli": {"ok": True},
    )
    out = tools.cobalt_scroll("tab-1", direction="down", amount=5)
    assert "down" in out
    assert "5" in out


def test_cobalt_close_returns_success(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service,
        "close_tab",
        lambda tab_id, user_id="algo-cli": {"ok": True},
    )
    out = tools.cobalt_close("tab-1")
    assert "Closed" in out


# ---------------------------------------------------------------------------
# Curated policy and action registry
# ---------------------------------------------------------------------------

def test_browser_tools_have_curated_policies():
    from algo_cli.marcus_authority import policy_for_action

    for name in (
        "cobalt_open",
        "cobalt_snapshot",
        "cobalt_screenshot",
        "cobalt_navigate",
        "cobalt_click",
        "cobalt_type",
        "cobalt_scroll",
        "cobalt_close",
    ):
        policy = policy_for_action(name)
        assert policy.curated, f"{name} should be curated"
        assert policy.effect_class.value == "observe", f"{name} should be observe-class"


def test_browser_tools_registered_in_action_specs():
    from algo_cli.action_registry import ACTION_SPECS

    names = {spec.name for spec in ACTION_SPECS}
    for name in (
        "cobalt_open",
        "cobalt_snapshot",
        "cobalt_screenshot",
        "cobalt_navigate",
        "cobalt_click",
        "cobalt_type",
        "cobalt_scroll",
        "cobalt_close",
    ):
        assert name in names, f"{name} missing from ACTION_SPECS"