from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "algo_cli/resources/neon_extension"


def _read(name: str) -> str:
    return (EXTENSION / name).read_text(encoding="utf-8")


def test_manifest_has_only_the_four_reviewed_permissions_and_no_ambient_access() -> None:
    manifest = json.loads(_read("neon_manifest_template.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == [
        "activeTab",
        "scripting",
        "nativeMessaging",
        "storage",
    ]
    assert manifest["incognito"] == "not_allowed"
    assert manifest["background"] == {"service_worker": "neon_service_worker.js"}
    for forbidden in (
        "host_permissions",
        "optional_host_permissions",
        "content_scripts",
        "externally_connectable",
        "web_accessible_resources",
        "sandbox",
        "key",
    ):
        assert forbidden not in manifest
    assert not {
        "tabs",
        "cookies",
        "history",
        "downloads",
        "debugger",
        "webNavigation",
        "webRequest",
        "declarativeNetRequest",
        "management",
        "proxy",
    } & set(manifest["permissions"])


def test_manifest_csp_allows_only_packaged_script_and_no_objects() -> None:
    manifest = json.loads(_read("neon_manifest_template.json"))
    csp = manifest["content_security_policy"]["extension_pages"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "unsafe-eval" not in csp
    assert "unsafe-inline" not in csp
    assert "http:" not in csp and "https:" not in csp


def test_service_worker_is_observe_only_and_has_no_dynamic_execution_or_raw_cdp() -> None:
    source = _read("neon_service_worker.js")
    forbidden_fragments = (
        "chrome.debugger",
        "chrome.downloads",
        "chrome.cookies",
        "chrome.history",
        "chrome.webRequest",
        "eval(",
        "new Function",
        ".click(",
        ".focus(",
        "dispatchEvent(",
        "innerHTML =",
        "outerHTML",
        "document.cookie",
        "localStorage",
        "indexedDB",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket(",
    )
    for fragment in forbidden_fragments:
        assert fragment not in source
    assert 'func: observeTopDocument' in source
    assert 'world: "ISOLATED"' in source
    assert 'frameIds: [0]' in source
    assert 'mode !== "observe_only"' in source
    assert 'NEON_COMMANDS = new Set(["status", "connect", "disconnect"])' in source


def test_user_gesture_navigation_worker_restart_and_native_disconnect_revoke_state() -> None:
    source = _read("neon_service_worker.js")
    assert 'default_popup": "neon_popup.html"' in _read("neon_manifest_template.json")
    assert 'connectButton.addEventListener("click"' in _read("neon_popup.js")
    assert "crypto.randomUUID()" in source
    assert 'changeInfo.status !== "loading"' in source
    assert 'revoke("navigation_revoked")' in source
    assert 'revoke("tab_closed")' in source
    assert 'revoke("service_worker_restarted")' not in source
    assert '"service_worker_restarted"' in source
    assert 'revoke("native_disconnected")' in source
    assert 'chrome.storage.session' in source
    assert 'chrome.storage.local' not in source
    assert 'chrome.storage.sync' not in source


def test_injected_observation_is_structural_bounded_and_never_collects_page_text() -> None:
    source = _read("neon_service_worker.js")
    assert "innerText" not in source
    assert "textContent" not in source
    assert "document.documentElement" not in source
    assert "document.body.innerHTML" not in source
    assert "outerHTML" not in source
    assert "secure_field_count" in source
    assert "upload_control_count" in source
    assert "canvas_count" in source
    assert "frame_count" in source
    assert "shadow_host_count" in source
    assert ".slice(0, 33)" in source
    # All selectors are fixed packaged literals; no message- or model-derived
    # value flows into querySelector/querySelectorAll.
    selector_calls = re.findall(r"querySelector(?:All)?\(([^\n]+)\)", source)
    assert selector_calls
    assert all(argument.lstrip().startswith(("'", '"')) for argument in selector_calls)


def test_popup_uses_text_content_and_contains_no_inline_event_or_remote_resource() -> None:
    html = _read("neon_popup.html")
    script = _read("neon_popup.js")
    assert "onclick=" not in html
    assert "onload=" not in html
    assert "<script src=\"neon_popup.js\"></script>" in html
    assert "http://" not in html and "https://" not in html
    assert "textContent" in script
    assert "innerHTML" not in script


def test_extension_resource_set_is_closed_and_contains_no_source_maps_or_binaries() -> None:
    names = {path.name for path in EXTENSION.iterdir() if path.is_file()}
    assert names == {
        "neon_manifest_template.json",
        "neon_popup.html",
        "neon_popup.js",
        "neon_service_worker.js",
    }
    assert all(path.stat().st_size <= 32_768 for path in EXTENSION.iterdir() if path.is_file())
