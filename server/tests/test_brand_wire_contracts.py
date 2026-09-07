"""The MCP tool prefix gates a fail-closed permission registry (spec §4 risk 2).
Five hand-typed copies once existed; this pins them to one constant so the Phase 2
flip is a one-line change that cannot desynchronise the server name from a matcher.

Since Phase 2a the same gate also guards the LEGACY spellings. Those are not dead
strings to be swept up later — they are the permanent accept side of a dual-accept
contract (markers and tool names live in stored transcripts that are re-parsed on
replay). A second hand-typed copy of a compat matcher is worse than a copy of the
emitted one: it fails closed, so a matcher that missed a prefix downgrades a
DESTRUCTIVE tool's card instead of raising an error anyone would notice.
"""
import ast
import re
from pathlib import Path

from ccswitch import brand
from server.assistant import tools
from server.assistant.options import assistant_options
from server.sessions import events

REPO_ROOT = Path(__file__).resolve().parents[2]
#: web/e2e is in scope too: a Playwright spec that hand-types a wire name is a
#: fifth copy exactly like a server one, and it is the surface least likely to be
#: caught by a type error when the name changes.
_SCAN = (("server", "*.py"), ("scripts", "*.py"), ("src", "*.py"),
         ("web/src", "*.ts"), ("web/src", "*.tsx"), ("web/e2e", "*.ts"))
#: Files that OWN a spelling, and so may legitimately contain the literals.
_BRAND_MODULES = ("src/ccswitch/brand.py", "web/src/brand.ts")
#: Any wire string, in EITHER spelling. The new one is gated as tightly as the old:
#: the failure this prevents is a hand-typed copy drifting from the constant, and a
#: copy of the current spelling drifts just as readily as a copy of the legacy one.
_WIRE = (brand.MCP_TOOL_PREFIX, brand.LEGACY_MCP_TOOL_PREFIX,
         f"<!-- {brand.SLUG}:", f"<!-- {brand.LEGACY_SLUG}:",
         f"<{brand.SLUG}-context ", f"<{brand.LEGACY_SLUG}-context ")

# Prose is exempt on purpose, and the scan is written to make that structural rather
# than a list of pardons. A comment or docstring that spells `<!-- ccswitch:complete -->`
# is DOCUMENTING a contract that is now permanent — the accept side never comes out —
# and banning the words would push the explanation out of the code that implements it.
# Only string literals that could reach the wire are offenders, so Python is scanned
# through the AST (comments are not nodes; docstrings are skipped explicitly) and
# TypeScript with its comments stripped.
_TS_COMMENT = re.compile(r"//.*$|/\*.*?\*/", re.MULTILINE | re.DOTALL)


def _py_string_literals(source: str) -> list[str]:
    """Every str constant in *source* except docstrings."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


_TS_STRING = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`")


def _ts_string_literals(source: str) -> list[str]:
    """Every quoted run in comment-stripped *source*. Deliberately loose: a false
    positive here is a one-line fix, a false negative is a fifth copy of a wire name."""
    return [part for match in _TS_STRING.findall(_TS_COMMENT.sub("", source))
            for part in match if part]


def test_prefix_constants_are_one_value():
    assert tools.MCP_PREFIX == brand.MCP_TOOL_PREFIX
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"
    assert brand.LEGACY_MCP_TOOL_PREFIX == f"mcp__{brand.LEGACY_MCP_SERVER_NAME}__"
    assert brand.MCP_TOOL_PREFIXES == (brand.MCP_TOOL_PREFIX, brand.LEGACY_MCP_TOOL_PREFIX)


def test_app_action_kind_is_decided_by_the_brand_helper():
    """Behavioural replacement for the old ``events._APP_ACTION_PREFIX`` constant:
    the classifier now routes through ``brand.is_mcp_tool``, so pin the behaviour
    rather than re-introducing an alias that only a test reads."""
    for prefix in brand.MCP_TOOL_PREFIXES:
        assert events.tool_kind(prefix + "list_sessions") is events.ToolKind.APP_ACTION
    assert events.tool_kind("mcp__github__list_sessions") is events.ToolKind.MCP


def _is_test(path: Path) -> bool:
    return "tests" in path.parts or path.name.endswith((".test.ts", ".test.tsx", ".spec.ts"))


def test_no_hand_typed_wire_string_in_production_code():
    """No module but the brand modules may spell a wire string.

    Tests are exempt, and that is the intended rule rather than an oversight: a test
    that pins ``COMPLETION_MARKER == "<!-- viberpit:complete -->"`` against a hand-typed
    literal is doing its job — deriving the expected value from the same constant it is
    checking would make the assertion tautological. What the gate exists to stop is
    PRODUCTION code growing a second copy that can drift from ``brand``, which is the
    failure Phase 0a was written to make impossible and this phase must not undo.
    """
    offenders = []
    exempt = {REPO_ROOT / rel for rel in _BRAND_MODULES}
    for scan_dir, glob in _SCAN:
        root = REPO_ROOT / scan_dir
        if not root.exists():
            continue
        for path in root.rglob(glob):
            if path in exempt or _is_test(path) or "node_modules" in path.parts:
                continue
            source = path.read_text()
            literals = (_py_string_literals(source) if path.suffix == ".py"
                        else _ts_string_literals(source))
            for literal in literals:
                if any(wire in literal for wire in _WIRE):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {literal[:80]!r}")
    assert offenders == [], offenders


def test_the_gate_would_catch_a_hand_typed_copy():
    """The scan is only worth having if it actually fires — pin that, so a refactor
    of the literal extraction cannot silently turn the gate into a no-op."""
    py = f'X = "{brand.LEGACY_MCP_TOOL_PREFIX}list_sessions"\n'
    assert any(brand.LEGACY_MCP_TOOL_PREFIX in lit for lit in _py_string_literals(py))
    ts = f"const x = '{brand.MCP_TOOL_PREFIX}list_sessions'\n"
    assert any(brand.MCP_TOOL_PREFIX in lit for lit in _ts_string_literals(ts))
    # ...and that prose is exempt structurally, not by a pardon list.
    assert not _py_string_literals(f'"""doc {brand.LEGACY_MCP_TOOL_PREFIX} x"""\n')
    assert not _ts_string_literals(f"// see '{brand.MCP_TOOL_PREFIX}list_sessions'\n")


def test_web_brand_matches_python_brand():
    """web/src/brand.ts is the web mirror of src/ccswitch/brand.py — the two must
    never disagree on the wire-visible name (spec §4 risk 2). Both halves of the
    dual-accept are checked: a web app that dropped the legacy prefix would render
    replayed pre-rebrand tool calls as raw `mcp__` names."""
    ts = (REPO_ROOT / "web/src/brand.ts").read_text()
    name_m = re.search(r"export const BRAND_NAME = '([^']*)'", ts)
    prefix_m = re.search(r"export const MCP_TOOL_PREFIX = '([^']*)'", ts)
    legacy_m = re.search(r"export const LEGACY_MCP_TOOL_PREFIX = '([^']*)'", ts)
    assert name_m, "BRAND_NAME export not found in web/src/brand.ts"
    assert prefix_m, "MCP_TOOL_PREFIX export not found in web/src/brand.ts"
    assert legacy_m, "LEGACY_MCP_TOOL_PREFIX export not found in web/src/brand.ts"
    assert name_m.group(1) == brand.NAME
    assert prefix_m.group(1) == brand.MCP_TOOL_PREFIX
    assert legacy_m.group(1) == brand.LEGACY_MCP_TOOL_PREFIX


def test_assistant_options_mount_the_one_mcp_server_name():
    """Behavioural twin of the string scan: the actual options dict handed to the
    SDK must mount the server under brand.MCP_SERVER_NAME, and the wire tool name
    built from brand.MCP_TOOL_PREFIX must resolve back to a real TOOL_SPECS entry."""
    options = assistant_options(server=object(), max_turns=1, model="haiku")
    assert list(options["mcp_servers"].keys()) == [brand.MCP_SERVER_NAME]
    assert tools.annotation_for(brand.MCP_TOOL_PREFIX + "list_sessions") is not None
