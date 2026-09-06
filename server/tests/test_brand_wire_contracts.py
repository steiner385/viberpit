"""The MCP tool prefix gates a fail-closed permission registry (spec §4 risk 2).
Five hand-typed copies once existed; this pins them to one constant so the Phase 2
flip is a one-line change that cannot desynchronise the server name from a matcher."""
import re
from pathlib import Path

from ccswitch import brand
from server.assistant import tools
from server.assistant.options import assistant_options
from server.sessions import events

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("server", "scripts", "src")
_STRAY = ('"mcp__ccswitch__"', "'mcp__ccswitch__'",
          'create_sdk_mcp_server(name="ccswitch"')


def test_prefix_constants_are_one_value():
    assert tools.MCP_PREFIX == brand.MCP_TOOL_PREFIX
    assert events._APP_ACTION_PREFIX == brand.MCP_TOOL_PREFIX
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"


def test_no_hand_typed_prefix_in_server_code():
    offenders = []
    brand_py = REPO_ROOT / "src" / "ccswitch" / "brand.py"
    for scan_dir in _SCAN_DIRS:
        for path in (REPO_ROOT / scan_dir).rglob("*.py"):
            if "tests" in path.parts or path == brand_py:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if any(s in line for s in _STRAY):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert offenders == [], offenders


def test_web_brand_matches_python_brand():
    """web/src/brand.ts is the web mirror of src/ccswitch/brand.py — the two must
    never disagree on the wire-visible name (spec §4 risk 2)."""
    ts = (REPO_ROOT / "web/src/brand.ts").read_text()
    name_m = re.search(r"export const BRAND_NAME = '([^']*)'", ts)
    prefix_m = re.search(r"export const MCP_TOOL_PREFIX = '([^']*)'", ts)
    assert name_m, "BRAND_NAME export not found in web/src/brand.ts"
    assert prefix_m, "MCP_TOOL_PREFIX export not found in web/src/brand.ts"
    assert name_m.group(1) == brand.NAME
    assert prefix_m.group(1) == brand.MCP_TOOL_PREFIX


def test_assistant_options_mount_the_one_mcp_server_name():
    """Behavioural twin of the string scan: the actual options dict handed to the
    SDK must mount the server under brand.MCP_SERVER_NAME, and the wire tool name
    built from brand.MCP_TOOL_PREFIX must resolve back to a real TOOL_SPECS entry."""
    options = assistant_options(server=object(), max_turns=1, model="haiku")
    assert list(options["mcp_servers"].keys()) == [brand.MCP_SERVER_NAME]
    assert tools.annotation_for(brand.MCP_TOOL_PREFIX + "list_sessions") is not None
