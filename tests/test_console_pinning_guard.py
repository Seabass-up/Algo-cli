"""Recording Rich consoles in tests must not inherit the host's platform probes.

On Windows runners an unpinned Console detects legacy Windows and a cp1252 stdout,
substitutes ASCII box glyphs and drops colour, so text assertions that pass on
macOS fail only in CI. Build recorders with the `recording_console` helper
(tests/_consoles.py) or pin `file=` and `legacy_windows=` explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_EXPORTS = frozenset({"export_text", "export_html", "export_svg", "save_text", "save_html", "save_svg"})
# Rich console classes, matched by bare name or attribute (`rich.console.Console`, `display.RuntimeConsole`).
# Aliases and subclasses defined in the scanned file are added per file.
_CONSOLE_CLASSES = frozenset({"Console", "RuntimeConsole"})
_HOST_STREAMS = frozenset({"stdout", "stderr", "__stdout__", "__stderr__"})
_OPAQUE = object()

# (file, enclosing function) sites still awaiting migration. Entries may only be removed;
# a stale entry fails the guard so the list keeps shrinking.
_ALLOWED_UNPINNED: frozenset[tuple[str, str]] = frozenset()
# Sites that build an unpinned recorder on purpose to reproduce the runner failure.
_REPRODUCTIONS = frozenset({("test_simulated_windows.py", "test_simulation_reproduces_runner_glyph_substitution")})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _console_names(tree: ast.AST) -> set[str]:
    """Class names in this file that build a Rich console: imports, aliases and subclasses."""
    names = set(_CONSOLE_CLASSES)

    def refers(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in names) or (
            isinstance(node, ast.Attribute) and node.attr in names
        )

    changed = True
    while changed:
        before = len(names)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.asname for alias in node.names if alias.name in names and alias.asname)
            elif isinstance(node, ast.ClassDef) and any(refers(base) for base in node.bases):
                names.add(node.name)
            elif isinstance(node, ast.Assign) and refers(node.value):
                names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        changed = len(names) != before
    return names


def _arguments(call: ast.Call) -> tuple[dict[str, ast.AST], bool]:
    """Keyword arguments by name (expanding literal `**{...}`/`**dict(...)`) and whether an opaque spread remains."""
    arguments: dict[str, ast.AST] = {}
    opaque = False
    for keyword in call.keywords:
        if keyword.arg is not None:
            arguments[keyword.arg] = keyword.value
            continue
        value = keyword.value
        if isinstance(value, ast.Dict) and all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in value.keys
        ):
            arguments.update({key.value: item for key, item in zip(value.keys, value.values)})
        elif isinstance(value, ast.Call) and _dotted(value.func) == "dict" and not value.args:
            nested, nested_opaque = _arguments(value)
            arguments.update(nested)
            opaque = opaque or nested_opaque
        else:
            opaque = True
    return arguments, opaque


def _falsy_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def _records(arguments: dict[str, ast.AST], opaque: bool) -> bool:
    if "record" in arguments:
        return not _falsy_constant(arguments["record"])
    return opaque


def _pinned(arguments: dict[str, ast.AST]) -> bool:
    file = arguments.get("file")
    legacy = arguments.get("legacy_windows")
    if file is None or (isinstance(file, ast.Constant) and file.value is None):
        return False
    if isinstance(file, ast.Attribute) and file.attr in _HOST_STREAMS and _dotted(file.value) == "sys":
        return False
    # legacy_windows=None (or any computed value) still lets Rich probe the host.
    return isinstance(legacy, ast.Constant) and isinstance(legacy.value, bool)


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes that belong to this scope, excluding the bodies of nested functions."""
    nodes: list[ast.AST] = []
    stack = list(scope.body) if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else list(
        ast.iter_child_nodes(scope)
    )
    while stack:
        node = stack.pop()
        nodes.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Decorators and defaults run in the enclosing scope; the body does not.
            stack.extend([*node.decorator_list, *node.args.defaults, *filter(None, node.args.kw_defaults)])
        else:
            stack.extend(ast.iter_child_nodes(node))
    return nodes


def _bindings(call: ast.Call, parents: dict[int, ast.AST]) -> set[str]:
    parent = parents.get(id(call))
    targets: list[ast.AST] = []
    if isinstance(parent, ast.Assign):
        targets = list(parent.targets)
    elif isinstance(parent, (ast.AnnAssign, ast.NamedExpr)):
        targets = [parent.target]
    elif isinstance(parent, ast.withitem) and parent.optional_vars is not None:
        targets = [parent.optional_vars]
    return {name for name in map(_dotted, targets) if name}


def unpinned_recording_consoles(source: str) -> list[tuple[str, int]]:
    """Return (enclosing function, line) for recording console constructions that do not pin the host probes.

    A construction records when it passes a truthy `record=` (including through a literal `**{...}`), when an
    opaque `**kwargs` could carry one, or when the console it is bound to is exported in the same scope. It is
    pinned only when `file=` is neither None nor the host's sys stream and `legacy_windows=` is a literal bool.
    """
    tree = ast.parse(source)
    console_names = _console_names(tree)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    scopes: list[tuple[str, ast.AST]] = [("<module>", tree)] + [
        (node.name, node) for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    found: list[tuple[str, int]] = []
    for name, scope in scopes:
        nodes = _scope_nodes(scope)
        exported_names: set[str] = set()
        exported_calls: set[int] = set()
        for node in nodes:
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _EXPORTS:
                receiver = node.func.value
                exported_calls.add(id(receiver))
                dotted = _dotted(receiver)
                if dotted:
                    exported_names.add(dotted)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                (isinstance(func, ast.Name) and func.id in console_names)
                or (isinstance(func, ast.Attribute) and func.attr in console_names)
            ):
                continue
            arguments, opaque = _arguments(node)
            exported = id(node) in exported_calls or bool(_bindings(node, parents) & exported_names)
            if (_records(arguments, opaque) or exported) and not _pinned(arguments):
                found.append((name, node.lineno))
    return sorted(found, key=lambda item: item[1])


def _violations() -> dict[tuple[str, str], list[int]]:
    violations: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        relative = path.relative_to(_TESTS_DIR).as_posix()
        for function, line in unpinned_recording_consoles(path.read_text(encoding="utf-8")):
            violations.setdefault((relative, function), []).append(line)
    return violations


def test_recording_consoles_pin_file_and_legacy_windows():
    violations = _violations()
    unexpected = {site: lines for site, lines in violations.items() if site not in _ALLOWED_UNPINNED | _REPRODUCTIONS}
    assert not unexpected, (
        "Recording Rich consoles must pin file= and legacy_windows= (use the recording_console "
        f"helper from tests/_consoles.py): {unexpected}"
    )


def test_allow_list_only_shrinks():
    stale = (_ALLOWED_UNPINNED | _REPRODUCTIONS) - set(_violations())
    assert not stale, f"Remove migrated sites from _ALLOWED_UNPINNED: {sorted(stale)}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("def t():\n    c = Console(record=True, width=80)\n", [("t", 2)], id="record-unpinned"),
        pytest.param(
            "def t():\n    c = rich.console.Console(file=f, width=80)\n    c.export_text()\n",
            [("t", 2)],
            id="export-missing-legacy",
        ),
        pytest.param(
            "def t():\n    c = Console(record=True, file=f, legacy_windows=False)\n", [], id="record-pinned"
        ),
        pytest.param("def t():\n    c = Console(file=f)\n    print(c)\n", [], id="plain-console"),
        pytest.param("def t():\n    c = Console(record=False)\n", [], id="record-false"),
        pytest.param(
            "def outer():\n    def make():\n        return Console(record=True)\n    return make\n",
            [("make", 3)],
            id="nested-factory",
        ),
        pytest.param("c = Console(record=True, file=f)\n", [("<module>", 1)], id="module-level"),
        pytest.param(
            "c = Console(file=f)\ndef t():\n    r = recording_console()\n    r.export_text()\n",
            [],
            id="module-console-beside-unrelated-export",
        ),
        pytest.param(
            "def t():\n    c = Console(file=f)\n    r = recording_console()\n    r.export_text()\n",
            [],
            id="function-console-beside-unrelated-export",
        ),
        pytest.param("def t():\n    Console(file=f).export_text()\n", [("t", 2)], id="chained-export"),
        pytest.param(
            "def t():\n    with Console(file=f) as c:\n        pass\n    c.save_html(p)\n", [("t", 2)], id="with-export"
        ),
        pytest.param(
            "def t():\n    self.c = Console(**opts)\n    self.c.export_text()\n", [("t", 2)], id="attribute-export"
        ),
        pytest.param(
            "def t():\n    c = Console(record=True, file=None, legacy_windows=None)\n", [("t", 2)], id="none-pins"
        ),
        pytest.param(
            "def t():\n    c = Console(record=True, file=sys.stdout, legacy_windows=False)\n",
            [("t", 2)],
            id="host-stdout-pin",
        ),
        pytest.param(
            "def t():\n    c = Console(record=True, file=f, legacy_windows=flag)\n", [("t", 2)], id="computed-legacy"
        ),
        pytest.param(
            "def t():\n    c = display.RuntimeConsole(record=True)\n    c.export_text()\n",
            [("t", 2)],
            id="runtime-console-subclass",
        ),
        pytest.param(
            "from rich.console import Console as C\ndef t():\n    c = C(record=True)\n",
            [("t", 3)],
            id="aliased-import",
        ),
        pytest.param(
            "class Rec(Console):\n    pass\ndef t():\n    c = Rec(record=True, file=f)\n",
            [("t", 4)],
            id="local-subclass",
        ),
        pytest.param("def t():\n    c = Console(**{'record': True})\n", [("t", 2)], id="record-via-dict-spread"),
        pytest.param("def t():\n    c = Console(**dict(record=True))\n", [("t", 2)], id="record-via-dict-call"),
        pytest.param("def t():\n    c = Console(**opts)\n", [("t", 2)], id="opaque-spread"),
        pytest.param(
            "def t():\n    c = Console(**opts, file=f, legacy_windows=False)\n", [], id="opaque-spread-pinned"
        ),
        pytest.param("def t():\n    c = Console(**{'record': False}, file=f)\n", [], id="dict-spread-no-record"),
        pytest.param(
            "class _Console:\n    pass\ndef t():\n    c = _Console()\n    c.export_text()\n", [], id="unrelated-fake"
        ),
        pytest.param(
            "def t(c=Console(record=True)):\n    pass\n", [("<module>", 1)], id="default-argument-in-enclosing-scope"
        ),
    ],
)
def test_guard_detects_unpinned_recorders(source, expected):
    assert unpinned_recording_consoles(source) == expected
