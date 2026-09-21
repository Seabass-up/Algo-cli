"""Compact discovery must not remove runtime commands or bury advanced routes."""

import io

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from algo_cli import display, oliver_slash_dispatch as slash
from algo_cli.config import Config


def complete(text):
    completer = slash.SlashCommandCompleter(slash.SLASH_COMMANDS)
    return list(completer.get_completions(Document(text), CompleteEvent()))


def help_text(monkeypatch, topic="", width=100):
    output = io.StringIO()
    monkeypatch.setattr(display, "console", Console(file=output, width=width, theme=display.THEME_MAP["tokyo-night"]))
    display.show_help(topic)
    return output.getvalue()


def test_initial_menu_is_small_and_ordered():
    names = [item.text for item in complete("/")]
    assert names == list(slash.COMMON_SLASH_COMMANDS)
    assert len(names) == 12


def test_partial_root_does_not_expand_children_or_aliases():
    assert [item.text for item in complete("/har")] == ["/harness"]
    assert [item.text for item in complete("/int")] == ["/intuition", "/intelligence"]


def test_parent_space_reveals_only_its_children():
    names = [item.text for item in complete("/harness ")]
    assert "/harness refresh" in names
    assert "/harness rust" not in names
    assert all(name.startswith("/harness ") for name in names)
    assert not complete("/harness refresh something")


def test_help_category_completion_replaces_full_prefix():
    text = "/HELP kn"
    item, = complete(text)
    assert text[:len(text) + item.start_position] + item.text == "/help knowledge"


def test_all_roots_have_exactly_one_category():
    roots = {name.split()[0] for name, _ in slash.SLASH_COMMANDS}
    grouped = [root for group in slash.COMMAND_GROUPS.values() for root in group]
    assert len(grouped) == len(set(grouped))
    assert roots == set(grouped)


def test_default_help_is_compact_and_category_help_is_focused(monkeypatch):
    text = help_text(monkeypatch)
    assert "/help knowledge" in text
    assert "/help all" in text
    assert "/harness benchmark-embed" not in text
    assert len(text.splitlines()) < 40
    text = help_text(monkeypatch, "workspace")
    assert "/worktree new" in text
    assert "/ship push" in text
    assert "/remember" not in text


def test_exact_command_help_and_unknown_help(monkeypatch):
    text = help_text(monkeypatch, "/kernel")
    assert "/kernel check" in text
    assert "/memory-auto" not in text
    text = help_text(monkeypatch, "[not-a-topic]")
    assert "Unknown help topic: [not-a-topic]" in text


def test_help_dispatch_passes_category(monkeypatch):
    calls = []
    monkeypatch.setattr(display, "show_help", lambda topic="": calls.append(topic))
    assert slash.handle_command("/help knowledge", Config(), None)[0]
    assert calls == ["knowledge"]


def test_advanced_roots_remain_discoverable():
    for name, description in slash.SLASH_COMMANDS:
        if " " not in name and not slash.is_command_alias(name, description):
            assert name in [item.text for item in complete(name)]
