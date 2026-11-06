/**
 * Brand identity — the ONE place the product name is spelled in the web app.
 * Mirrors `src/viberpit/brand.py`; the two must agree (spec
 * docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §1).
 */

/** Display name: top bar, document titles, overlays, help copy. */
export const BRAND_NAME = 'ViberPit'

/**
 * The wordmark's two halves — "Viber" in ink, "Pit" in brand green — as the
 * logo draws them. Spelled here, beside `BRAND_NAME`, so the split cannot
 * drift from the name: `brand.test.ts` asserts they join back to it. Only
 * `Wordmark` (components/BrandMark.tsx) renders this; prose uses `BRAND_NAME`.
 */
export const BRAND_NAME_PARTS = ['Viber', 'Pit'] as const

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

/**
 * Per-repo namespace dirs a session cwd can sit under (clones, worktrees,
 * scratch). Emitted dot-dir first; the legacy one is matched permanently, since
 * a repo root that already had it keeps using it. New worktrees and scratch dirs
 * go under the non-hidden `viberpit-sessions` (#2934). Mirrors `brand.REPO_NAMESPACES`.
 */
export const REPO_NAMESPACES = ['.viberpit', '.ccswitch', 'viberpit-sessions'] as const
