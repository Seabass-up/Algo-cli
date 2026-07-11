"""Tests for the URL scheme handler (algo_cli.url_scheme)."""

from __future__ import annotations

from algo_cli import url_scheme


class TestParseDeepLink:
    def test_parse_skill_route(self):
        link = url_scheme.parse_deep_link("algo-cli://skill/echo-veil-integration")
        assert link is not None
        assert link.route == "skill"
        assert link.path == "echo-veil-integration"

    def test_parse_memory_recall_with_query(self):
        link = url_scheme.parse_deep_link("algo-cli://memory/recall?q=rebrand")
        assert link is not None
        assert link.route == "memory"
        assert link.path == "recall"
        assert link.query == {"q": "rebrand"}

    def test_parse_session_new(self):
        link = url_scheme.parse_deep_link("algo-cli://session/new")
        assert link is not None
        assert link.route == "session"
        assert link.path == "new"

    def test_parse_session_load_with_query(self):
        link = url_scheme.parse_deep_link("algo-cli://session/load?name=my-session")
        assert link is not None
        assert link.route == "session"
        assert link.path == "load"
        assert link.query == {"name": "my-session"}

    def test_parse_version_no_path(self):
        link = url_scheme.parse_deep_link("algo-cli://version")
        assert link is not None
        assert link.route == "version"
        assert link.path == ""

    def test_parse_plugin_route(self):
        link = url_scheme.parse_deep_link("algo-cli://plugin/my-plugin")
        assert link is not None
        assert link.route == "plugin"
        assert link.path == "my-plugin"

    def test_parse_credential_route(self):
        link = url_scheme.parse_deep_link("algo-cli://credential/ollama-cloud")
        assert link is not None
        assert link.route == "credential"
        assert link.path == "ollama-cloud"

    def test_parse_empty_url(self):
        assert url_scheme.parse_deep_link("") is None

    def test_parse_wrong_scheme(self):
        assert url_scheme.parse_deep_link("https://example.com") is None

    def test_parse_no_path(self):
        link = url_scheme.parse_deep_link("algo-cli://")
        assert link is not None
        assert link.route == ""
        assert link.path == ""

    def test_parse_preserves_raw_url(self):
        url = "algo-cli://skill/test-skill"
        link = url_scheme.parse_deep_link(url)
        assert link is not None
        assert link.raw_url == url

    def test_parse_multiple_query_params(self):
        link = url_scheme.parse_deep_link("algo-cli://memory/recall?q=rebrand&limit=10")
        assert link is not None
        assert link.query == {"q": "rebrand", "limit": "10"}


class TestValidateDeepLink:
    def test_valid_skill_link(self):
        link = url_scheme.ParsedDeepLink(route="skill", path="test")
        valid, err = url_scheme.validate_deep_link(link)
        assert valid
        assert err == ""

    def test_valid_version_link_no_path(self):
        link = url_scheme.ParsedDeepLink(route="version", path="")
        valid, err = url_scheme.validate_deep_link(link)
        assert valid

    def test_invalid_unknown_route(self):
        link = url_scheme.ParsedDeepLink(route="unknown", path="x")
        valid, err = url_scheme.validate_deep_link(link)
        assert not valid
        assert "Unknown route" in err

    def test_invalid_missing_path_when_required(self):
        link = url_scheme.ParsedDeepLink(route="skill", path="")
        valid, err = url_scheme.validate_deep_link(link)
        assert not valid
        assert "requires a path" in err

    def test_invalid_empty_route(self):
        link = url_scheme.ParsedDeepLink(route="", path="")
        valid, err = url_scheme.validate_deep_link(link)
        assert not valid
        assert "Empty route" in err


class TestHandleDeepLink:
    def test_handle_valid_skill(self):
        result = url_scheme.handle_deep_link("algo-cli://skill/my-skill")
        assert result["valid"] is True
        assert result["action"] == "skill"
        assert result["target"] == "my-skill"

    def test_handle_valid_memory_with_query(self):
        result = url_scheme.handle_deep_link("algo-cli://memory/recall?q=test")
        assert result["valid"] is True
        assert result["action"] == "memory"
        assert result["target"] == "recall"
        assert result["query"] == {"q": "test"}

    def test_handle_invalid_scheme(self):
        result = url_scheme.handle_deep_link("https://example.com")
        assert result["valid"] is False
        assert "algo-cli://" in result["error"]

    def test_handle_unknown_route(self):
        result = url_scheme.handle_deep_link("algo-cli://bogus/path")
        assert result["valid"] is False
        assert "Unknown route" in result["error"]

    def test_handle_missing_path(self):
        result = url_scheme.handle_deep_link("algo-cli://skill")
        assert result["valid"] is False
        assert "requires a path" in result["error"]

    def test_handle_version(self):
        result = url_scheme.handle_deep_link("algo-cli://version")
        assert result["valid"] is True
        assert result["action"] == "version"


class TestFormatHelp:
    def test_help_contains_scheme_name(self):
        h = url_scheme.format_help()
        assert "algo-cli://" in h

    def test_help_lists_all_routes(self):
        h = url_scheme.format_help()
        for route in url_scheme.KNOWN_ROUTES:
            assert route in h

    def test_help_has_examples(self):
        h = url_scheme.format_help()
        assert "Examples:" in h
        assert "skill" in h
