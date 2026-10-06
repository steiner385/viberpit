"""Stateless one-shot helpers are tool-free and transcript-free (#2706).

``test_helper_cli_probe_threads`` proves each known helper builds its options through
:func:`stateless_helper_options`; this file proves what those options mean on the CLI
command line, and guards against a NEW helper rebuilding the old ``allowed_tools=[]``
shape by hand.
"""
import ast
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from server.sessions.helper_call import stateless_helper_options

_SERVER = Path(__file__).resolve().parents[1]


def _argv(**kw) -> list[str]:
    opts = ClaudeAgentOptions(cli_path="/test/claude", **kw)
    return SubprocessCLITransport(prompt="x", options=opts)._build_command()


def test_options_render_to_tool_free_no_persistence_argv():
    argv = _argv(**stateless_helper_options())
    assert argv[argv.index("--tools") + 1] == ""          # no built-ins
    assert "--strict-mcp-config" in argv                   # no account MCP servers
    assert "--mcp-config" not in argv                      # ...and none of our own
    assert "--setting-sources=" in argv                    # no user/project/local settings
    assert "--no-session-persistence" in argv              # no native transcript


def test_legacy_shape_was_not_tool_free():
    """The shape this replaced leaves the CLI's default tool set and persistence on —
    the reason ``allowed_tools=[]`` is not an isolation guarantee."""
    argv = _argv(allowed_tools=[], setting_sources=[])
    assert "--tools" not in argv
    assert "--strict-mcp-config" not in argv
    assert "--no-session-persistence" not in argv


def test_each_call_gets_fresh_containers():
    a, b = stateless_helper_options(), stateless_helper_options()
    a["tools"].append("Bash")
    a["extra_args"]["oops"] = None
    assert b["tools"] == [] and b["extra_args"] == {"no-session-persistence": None}


def _unisolated_helper_calls(source: str) -> list[int]:
    """Lines of ``ClaudeAgentOptions(...)`` calls in a helper module (one that runs
    through ``run_helper_call``) that do not splat ``stateless_helper_options()``."""
    tree = ast.parse(source)
    if "run_helper_call" not in source:
        return []
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ClaudeAgentOptions"):
            continue
        splats = [kw.value for kw in node.keywords if kw.arg is None]
        if not any(isinstance(v, ast.Call) and getattr(v.func, "id", None) == "stateless_helper_options"
                   for v in splats):
            bad.append(node.lineno)
    return bad


def test_guard_flags_a_hand_rolled_helper():
    """The sweep below can fail: a helper that rebuilds the old shape is flagged."""
    src = ("from server.sessions.helper_call import run_helper_call\n"
           "o = ClaudeAgentOptions(model='haiku', allowed_tools=[], setting_sources=[])\n")
    assert _unisolated_helper_calls(src) == [2]
    ok = src.replace("allowed_tools=[], setting_sources=[]", "**stateless_helper_options()")
    assert _unisolated_helper_calls(ok) == []


def test_every_helper_module_isolates_its_options():
    offenders = {}
    scanned = 0
    for path in sorted(_SERVER.rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text()
        if "ClaudeAgentOptions(" in source and "run_helper_call" in source:
            scanned += 1
        bad = _unisolated_helper_calls(source)
        if bad:
            offenders[str(path.relative_to(_SERVER))] = bad
    assert scanned >= 10, "sweep found too few helper modules — is it scanning the tree?"
    assert offenders == {}, (
        "stateless helper options must splat stateless_helper_options() "
        f"(server/sessions/helper_call.py): {offenders}")
