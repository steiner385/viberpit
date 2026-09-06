"""The MCP tool prefix gates a fail-closed permission registry (spec §4 risk 2).
Five hand-typed copies once existed; this pins them to one constant so the Phase 2
flip is a one-line change that cannot desynchronise the server name from a matcher."""
from pathlib import Path

from ccswitch import brand
from server.assistant import tools
from server.sessions import events

SERVER_ROOT = Path(__file__).resolve().parents[1]
_STRAY = ('"mcp__ccswitch__"', "'mcp__ccswitch__'",
          'create_sdk_mcp_server(name="ccswitch"', '{"ccswitch": server}')


def test_prefix_constants_are_one_value():
    assert tools.MCP_PREFIX == brand.MCP_TOOL_PREFIX
    assert events._APP_ACTION_PREFIX == brand.MCP_TOOL_PREFIX
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"


def test_no_hand_typed_prefix_in_server_code():
    offenders = []
    for path in SERVER_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if any(s in line for s in _STRAY):
                offenders.append(f"{path.relative_to(SERVER_ROOT)}:{lineno}")
    assert offenders == [], offenders
