"""brand.py is the only module that spells the product name. These invariants are
what every later rename phase relies on."""
import re

from ccswitch import brand

_SLUG = re.compile(r"^[a-z][a-z0-9]*$")


def test_slug_is_lowercase_name():
    assert brand.SLUG == brand.NAME.lower()


def test_slugs_are_identifier_safe():
    for value in (brand.SLUG, brand.LEGACY_SLUG, brand.CLI_NAME, brand.HOST_SLUG,
                  brand.MCP_SERVER_NAME):
        assert _SLUG.match(value), value


def test_mcp_prefix_derives_from_server_name():
    # The SDK names tools `mcp__<server>__<tool>`; the prefix must never be typed by hand.
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"


def test_legacy_slug_is_frozen():
    # Compat readers key on this forever; it must not follow SLUG when SLUG flips.
    assert brand.LEGACY_SLUG == "ccswitch"
