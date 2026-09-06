import { MCP_TOOL_PREFIX } from '@/brand'

/** Presenter for `ToolKind.APP_ACTION` tool calls (the ccswitch MCP server
 * talking to itself — spec §4.4/§4.8). Keyed by the BARE tool name (the part
 * after the `mcp__ccswitch__` prefix), mirroring `TOOL_SPECS` in
 * server/assistant/tools.py — the two maps' KEY SETS must stay identical
 * (checked cross-language via appActions.test.ts + the fixture it shares with
 * server/tests/test_assistant_tools.py). Title text itself is independent:
 * the server side names tools for permission-card summaries ("List sessions"),
 * this side names completed actions for transcript rows ("Listed sessions"). */

export const APP_ACTION_TITLES: Record<string, string> = {
  list_sessions: 'Listed sessions',
  get_session: 'Got session',
  get_session_transcript: 'Read session transcript',
  get_tool_result: 'Read a tool result',
  export_session: 'Exported session transcript',
  search_sessions: 'Searched sessions',
  list_archived_sessions: 'Listed archived sessions',
  list_accounts: 'Listed accounts',
  list_hosts: 'Listed fleet hosts',
  list_prs: 'Listed pull requests',
  get_pipeline_health: 'Checked CI health',
  list_repos: 'Listed repositories',
  list_branches: 'Listed branches',
  list_stage_models: 'Read CI stage models',
  list_automations: 'Listed automations',
  get_automation_events: 'Checked automation events',
  get_deploy_status: 'Checked deploy status',
  list_adoptable_sessions: 'Listed adoptable sessions',
  suggest_next_prompt: 'Suggested a next prompt',
  list_notification_channels: 'Listed notification channels',
  list_notification_rules: 'Listed notification rules',
  get_quiet_hours: 'Checked quiet hours',
  test_notification: 'Previewed notification routing',
  list_initiatives: 'Listed initiatives',
  get_initiative: 'Got initiative',
  list_products: 'Listed products',
  get_product: 'Got product',
  get_product_deps: 'Checked product dependencies',
  get_priority_queue: 'Checked priority queue',
  list_triage: 'Listed triage proposals',
  get_onboarding_status: 'Checked onboarding status',
  get_automation_actionable: 'Checked automation actions',
  list_automation_sessions: 'Listed automation sessions',
  list_backlog_candidates: 'Listed backlog candidates',
  get_backlog_groups: 'Checked backlog groups',
  get_cicd_findings: 'Checked CI/CD findings',
  get_pr_doctor_consolidation: 'Checked PR consolidation',
  get_pr_doctor_job: 'Checked PR doctor job',
  preview_schedule: 'Previewed a schedule',
  report_gap: 'Recorded a capability gap',
  create_session: 'Started a session',
  send_prompt: 'Sent a prompt',
  interrupt_session: 'Interrupted session',
  answer_question: 'Answered a question',
  resolve_permission: 'Resolved a permission',
  set_model: 'Switched model',
  set_permission_mode: 'Changed permission mode',
  resolve_model_approval: 'Resolved model approval',
  archive_session: 'Archived session',
  unarchive_session: 'Unarchived session',
  archive_sessions: 'Archived sessions',
  unarchive_sessions: 'Unarchived sessions',
  resume_sessions: 'Resumed sessions',
  rotate_sessions: 'Rotated sessions',
  set_autopilot_paused: 'Changed autopilot pause',
  fix_pr: 'Fixed a pull request',
  set_automation_enabled: 'Changed automation',
  rename_session: 'Renamed session',
  move_in_queue: 'Reordered queued session',
  pin_account: 'Changed account pin',
  rotate_session: 'Rotated to a different account',
  reload_session: 'Reloaded session',
  adopt_session: 'Adopted an untracked session',
  create_notification_rule: 'Created notification rule',
  delete_notification_rule: 'Deleted notification rule',
  watch: 'Created a watch',
  list_watches: 'Listed watches',
  delete_watch: 'Deleted a watch',
  set_quiet_hours: 'Set quiet hours',
  advance_initiative: 'Advanced initiative',
  resolve_triage: 'Resolved triage proposal',
  request_product_sync: 'Requested product sync',
  delete_session: 'Deleted session permanently',
  delete_sessions: 'Deleted sessions permanently',
  cancel_queued_session: 'Cancelled queued session',
  ready_merge_pr: 'Readied + auto-merged PR',
  trigger_automation_action: 'Triggered an automation action',
  dispatch_backlog: 'Dispatched backlog work',
  consolidate_prs: 'Started a PR consolidation',
  // Operator tier (spec v2 §7, Pack O) — only ever called when operator tools are
  // enabled (server/assistant/policy.py:operator_tools_enabled).
  start_deploy: 'Started a deploy',
  upgrade_host: 'Upgraded host',
  refresh_account: 'Refreshed account',
  set_account_enabled: 'Changed account enabled state',
  start_reauth: 'Started account re-auth',
}

/** `fullToolName` is the wire name, e.g. `mcp__ccswitch__list_sessions`. Returns
 * `undefined` for anything not in the map (including non-ccswitch MCP tools) so
 * callers can fall back to the raw name. */
export function appActionTitle(fullToolName: string): string | undefined {
  if (!fullToolName.startsWith(MCP_TOOL_PREFIX)) return undefined
  return APP_ACTION_TITLES[fullToolName.slice(MCP_TOOL_PREFIX.length)]
}
