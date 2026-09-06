/**
 * Brand identity — the ONE place the product name is spelled in the web app.
 * Mirrors `src/ccswitch/brand.py`; the two must agree (spec
 * docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §1).
 */

/** Display name: top bar, document titles, overlays, help copy. */
export const BRAND_NAME = 'ViberPit'

/**
 * Wire prefix of the app's own MCP tools (`mcp__<server>__<tool>`). A contract
 * with the server's `brand.MCP_SERVER_NAME`; flips in rebrand Phase 2 with a
 * dual-accept window — never edit it on its own.
 */
export const MCP_TOOL_PREFIX = 'mcp__ccswitch__'
