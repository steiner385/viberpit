/**
 * The Settings map's METADATA — what a section is called, where it sits and
 * what it is for — with no component attached.
 *
 * Split out of `settingsDefs.ts` (D-10) because that file eagerly imports every
 * section component, and the ⌘K palette (`features/palette`) needs the names
 * and keywords without dragging twenty-two sections into its chunk. This is the
 * ONE source of the metadata: `settingsDefs` imports it and adds the component
 * and the page-only fields; nothing else declares a section's label.
 *
 * The nav ICON is a page field, not metadata (D-10 ruling): the palette never
 * draws one, and importing twenty-two icons here split them into shared chunks
 * on the initial path. It lives beside the component in `settingsDefs`.
 */

export type SettingsGroupId = 'workspace' | 'sessions' | 'capacity' | 'claude' | 'about'
export interface SettingsGroupDef { id: SettingsGroupId; label: string }

/** Nav order. Frequency-first (spec §5): weekly changes above yearly ones. */
export const SETTINGS_GROUPS: readonly SettingsGroupDef[] = [
  { id: 'workspace', label: 'Workspace' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'capacity', label: 'Capacity' },
  { id: 'claude', label: 'Claude config' },
  { id: 'about', label: 'About' },
]

interface SettingsMetaShape {
  id: string
  group: SettingsGroupId
  label: string
  /** One sentence under the section title. Required: a section without one does not compile. */
  description: string
  keywords?: readonly string[]
}

/** Ids are URL segments and are stable: every id that existed before the
 *  Settings map did is kept. Order is nav order. */
const META = [
  { id: 'general',       group: 'workspace', label: 'General',
    description: 'Where archives are stored, how many archived sessions are kept, and the AI assistant.' },
  { id: 'appearance',    group: 'workspace', label: 'Appearance',
    description: 'Theme, navigation and layout for this browser only.', keywords: ['theme', 'dark', 'light'] },
  // Notifications, Channels and Routing rules were one section until #2077: two
  // CRUD lists under two forms is sibling sections, not one column (PAGE-TABS §2).
  // Only push is per-device; channels and rules are server state.
  { id: 'notifications', group: 'workspace', label: 'Notifications',
    description: 'Push to this device, and the quiet hours when only critical notifications get through.', keywords: ['push', 'quiet hours'] },
  { id: 'channels',      group: 'workspace', label: 'Channels',
    description: 'Webhook endpoints a routing rule can send a notification to.', keywords: ['webhook', 'slack', 'notifications'] },
  { id: 'rules',         group: 'workspace', label: 'Routing rules',
    description: 'Which events notify, and whether they push, go to a channel or are ignored.', keywords: ['notifications', 'routing', 'critical'] },
  { id: 'sessions',      group: 'sessions',  label: 'Defaults',
    // Names the three groups AND keeps the section's own "these are defaults, not
    // locks" caveat, which the registry sentence had dropped.
    description: 'What every new session starts with — tool approvals, model gating, transcript display and where its worktree goes — and a session can change any of them for itself once it is running.', keywords: ['permission', 'model', 'transcript', 'worktree', 'repo root', 'branch prefix', 'sandbox'] },
  { id: 'presets',       group: 'sessions',  label: 'Presets',
    description: 'Saved launch configurations for the New session dialog.' },
  { id: 'templates',     group: 'sessions',  label: 'Templates',
    description: 'Reusable prompt templates with {{variable}} placeholders.' },
  { id: 'profiles',      group: 'sessions',  label: 'Permission profiles',
    description: 'Named tool-permission sets a session can adopt.' },
  { id: 'always-allow',  group: 'sessions',  label: 'Always-allow rules',
    description: 'Standing permission rules created from session cards, with who made each and how often it fired.', keywords: ['permission', 'allow', 'rule'] },
  // What the fleet can run (spec 2026-09-20 D1/D2): supply, not spend. Delivery →
  // Capacity is the other half — how automation spends it — and the two link.
  { id: 'capacity',      group: 'capacity',  label: 'Limits',
    description: 'The global cap as its two ceilings — what the hosts can run and how much you want running — plus the env-managed limits outside the stack.', keywords: ['global cap', 'attention', 'physical', 'max active sessions'] },
  { id: 'fleet',         group: 'capacity',  label: 'Hosts',
    description: 'Machines that can run sessions: each host’s cap, dispatch weight and health.' },
  { id: 'accounts',      group: 'capacity',  label: 'Accounts',
    description: 'Claude Max and Copilot accounts the rotation can pick from.' },
  // D-13: first in the group — the user's own instructions change more often than
  // what is installed.
  { id: 'memory',        group: 'claude',    label: 'Memory',
    description: 'Your user memory file, the CLAUDE.md every account loads into every session.', keywords: ['CLAUDE.md', 'instructions', 'user memory'] },
  // D-16a: beside Memory — both shape how every session answers.
  { id: 'output-styles', group: 'claude',    label: 'Output styles',
    description: 'The style new sessions start with, and the styles every account can use.' },
  { id: 'plugins',       group: 'claude',    label: 'Plugins',
    description: 'Installed Claude Code plugins and what each one adds.' },
  { id: 'skills',        group: 'claude',    label: 'Skills',
    description: 'Skills available to every session, with an AI draft helper.' },
  { id: 'commands',      group: 'claude',    label: 'Commands',
    description: 'Slash commands available to every session.' },
  { id: 'mcp',           group: 'claude',    label: 'MCP servers',
    description: 'Model Context Protocol servers sessions can call, with OAuth where needed.' },
  { id: 'hooks',         group: 'claude',    label: 'Hooks',
    description: 'Shell commands Claude Code runs on lifecycle events. They run on this host.' },
  { id: 'snapshots',     group: 'claude',    label: 'Snapshots',
    description: 'Point-in-time copies of the shared Claude config, with diff, restore, export and import.' },
  // Sits in the About group beside the version it changes. It is also the phone's
  // ONLY deploy surface — `shell/useAppBanners` hides the deploy banner there.
  { id: 'deploy',        group: 'about',     label: 'Deploy',
    description: 'Commits waiting to ship to this server, and how the last deploy went.', keywords: ['ship', 'release', 'pending', 'update'] },
  { id: 'about',         group: 'about',     label: 'About',
    description: 'Version, host, and the server settings managed by environment variables.' },
] as const satisfies readonly SettingsMetaShape[]

export type SettingsSectionId = (typeof META)[number]['id']
export interface SettingsMeta extends SettingsMetaShape { id: SettingsSectionId }

export const SETTINGS_META: readonly SettingsMeta[] = META
export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = 'general'

const IDS: ReadonlySet<string> = (() => {
  const out = new Set<string>()
  for (const m of SETTINGS_META) {
    if (out.has(m.id)) throw new Error(`settingsMeta: duplicate id ${m.id}`)
    out.add(m.id)
  }
  return out
})()

export function isSettingsSectionId(x: string | null | undefined): x is SettingsSectionId {
  return typeof x === 'string' && IDS.has(x)
}
