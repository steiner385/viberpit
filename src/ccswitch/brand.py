"""Brand identity — the ONE place the product name is spelled.

Display strings, User-Agents, notification titles, and filenames derive from
``NAME`` / ``SLUG``. Wire contracts that cannot change without a dual-accept
window are pinned to ``LEGACY_SLUG`` here and flip in their own phase (see
docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3). Nothing outside
this module may spell the brand.
"""
from __future__ import annotations

#: Display name — UI, page titles, prose, push-notification titles, GitHub comments.
NAME = "ViberPit"
#: Lowercase identifier stem — User-Agent product, download filenames, new slugs.
SLUG = "viberpit"
#: The pre-rebrand slug. Only compat readers (parsers, path fallbacks) may use it.
LEGACY_SLUG = "ccswitch"

#: Environment-variable prefix. Both prefixes are live for the whole rebrand:
#: ``ccswitch.env_compat.mirror_legacy_env()`` fills each from the other at process
#: start, so an ``/etc`` env file may spell either one. The reverse direction retires
#: in Phase 7, once every host's env file is confirmed migrated.
ENV_PREFIX = f"{SLUG.upper()}_"
#: The pre-rebrand environment-variable prefix, still read everywhere.
LEGACY_ENV_PREFIX = f"{LEGACY_SLUG.upper()}_"

#: The installed console script. CLI message prefixes name the binary the user ran,
#: so this flips in Phase 5 when the ``viberpit`` entry point is added.
CLI_NAME = LEGACY_SLUG
#: Stem of /etc, /run, /var/lib, /var/log paths and systemd units. Flips in Phase 3
#: together with the unit files and the per-host migration runbook.
HOST_SLUG = LEGACY_SLUG
#: MCP server wire name. The SDK derives tool names ``mcp__<name>__<tool>`` from it,
#: and the permission registry string-matches the prefix, so it flips in Phase 2
#: with a dual-accept window — never on its own.
MCP_SERVER_NAME = LEGACY_SLUG
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
