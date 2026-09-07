/**
 * Brand identity — the ONE place the product name is spelled in the web app.
 * Mirrors `src/ccswitch/brand.py`; the two must agree (spec
 * docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §1).
 */

/** Display name: top bar, document titles, overlays, help copy. */
export const BRAND_NAME = 'ViberPit'

/**
 * Wire prefix of the app's own MCP tools (`mcp__<server>__<tool>`). A contract
 * with the server's `brand.MCP_SERVER_NAME` — never edit it on its own.
 */
export const MCP_TOOL_PREFIX = 'mcp__viberpit__'

/**
 * The pre-rebrand prefix, still matched by every reader — permanently. The web
 * app renders REPLAYED transcripts, so tool calls written before the rebrand
 * keep their old names for as long as those sessions exist. Mirrors
 * `brand.LEGACY_MCP_TOOL_PREFIX` on the server.
 */
export const LEGACY_MCP_TOOL_PREFIX = 'mcp__ccswitch__'

/**
 * The bare tool name if `fullToolName` is one of ours under EITHER prefix, else
 * `undefined`. The one place the web spells the dual-accept — mirrors
 * `brand.mcp_tool_stem()`.
 */
export function mcpToolStem(fullToolName: string): string | undefined {
  for (const prefix of [MCP_TOOL_PREFIX, LEGACY_MCP_TOOL_PREFIX]) {
    if (fullToolName.startsWith(prefix)) return fullToolName.slice(prefix.length)
  }
  return undefined
}
