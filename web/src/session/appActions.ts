/** Presenter for `ToolKind.APP_ACTION` tool calls (the ccswitch MCP server
 * talking to itself — spec §4.4/§4.8). Keyed by the BARE tool name (the part
 * after the `mcp__ccswitch__` prefix), mirroring `TOOL_SPECS` in
 * server/assistant/tools.py — the two maps' KEY SETS must stay identical
 * (checked cross-language via appActions.test.ts + the fixture it shares with
 * server/tests/test_assistant_tools.py). Title text itself is independent:
 * the server side names tools for permission-card summaries ("List sessions"),
 * this side names completed actions for transcript rows ("Listed sessions"). */
const MCP_CCSWITCH_PREFIX = 'mcp__ccswitch__'

export const APP_ACTION_TITLES: Record<string, string> = {
  list_sessions: 'Listed sessions',
  get_session: 'Got session',
  get_session_transcript: 'Read session transcript',
  search_sessions: 'Searched sessions',
  list_archived_sessions: 'Listed archived sessions',
  list_accounts: 'Listed accounts',
  list_hosts: 'Listed fleet hosts',
  list_prs: 'Listed pull requests',
  get_pipeline_health: 'Checked CI health',
  list_automations: 'Listed automations',
  get_automation_events: 'Checked automation events',
  get_deploy_status: 'Checked deploy status',
}

/** `fullToolName` is the wire name, e.g. `mcp__ccswitch__list_sessions`. Returns
 * `undefined` for anything not in the map (including non-ccswitch MCP tools) so
 * callers can fall back to the raw name. */
export function appActionTitle(fullToolName: string): string | undefined {
  if (!fullToolName.startsWith(MCP_CCSWITCH_PREFIX)) return undefined
  return APP_ACTION_TITLES[fullToolName.slice(MCP_CCSWITCH_PREFIX.length)]
}
