import type * as React from 'react'
import {
  SlidersHorizontal, Palette, Bell, Play, Bookmark, FileText, ShieldCheck,
  Users, Server, Puzzle, Sparkles, SquareTerminal, Plug, Webhook, Camera, Info,
  Rocket, Gauge, Send, Route,
  type LucideIcon,
} from 'lucide-react'
import type { SettingsScope } from './scope'
import { GeneralSection } from './sections/GeneralSection'
import { LimitsSection } from './sections/LimitsSection'
import { AppearanceSection } from './sections/AppearanceSection'
import { SessionDefaultsSection } from './sections/SessionDefaultsSection'
import { AboutSection } from './sections/AboutSection'
import { DeploySection } from './sections/DeploySection'
import { AccountsManager } from '@/features/accounts/AccountsManager'
import { NotificationsSection } from './sections/NotificationsSection'
import { NotificationChannelsSection } from './sections/NotificationChannelsSection'
import { NotificationRulesSection } from './sections/NotificationRulesSection'
import { HooksSection } from './sections/HooksSection'
import { SnapshotsSection } from './sections/SnapshotsSection'
import { FleetSection } from './sections/FleetSection'
import { PresetsSection } from './sections/PresetsSection'
import { TemplatesSection } from './sections/TemplatesSection'
import { PermissionProfilesSection } from './sections/PermissionProfilesSection'
import { PluginsSection } from './sections/PluginsSection'
import { SkillsSection } from './sections/SkillsSection'
import { CommandsSection } from './sections/CommandsSection'
import { McpSection } from './sections/McpSection'

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

export type SettingsBadgeKey = 'accountsAttention' | 'deployPending'

interface SettingsDefShape {
  id: string
  group: SettingsGroupId
  label: string
  icon: LucideIcon
  scope: SettingsScope
  /** One sentence under the section title. Required: a section without one does not compile. */
  description: string
  keywords?: readonly string[]
  badge?: SettingsBadgeKey
  /** Mount the live capacity readout after the body, with this stack row marked current (spec 2026-09-20 D10). */
  readout?: 'hosts' | 'accounts'
  /**
   * The component instantiates `SettingsSection` itself, so `SettingsPage`
   * renders it BARE. Everything else is wrapped in `HostedSection`, which builds
   * the chassis from this row. Set it on a section in the same change that
   * migrates it (PRs 5b–5e); when the last one is set, `HostedSection` and this
   * flag both go.
   */
  ownsChassis?: true
  component: React.ComponentType
}

/**
 * The one Settings map (spec §6.2). SettingsNav, the phone list, the route
 * fallback and pageContract.test.tsx all derive from this array. Ids are URL
 * segments and are stable: every id that existed before this file did is kept.
 */
const DEFS = [
  { id: 'general',       group: 'workspace', label: 'General',             icon: SlidersHorizontal, scope: 'server',  component: GeneralSection, ownsChassis: true,
    description: 'Where repos are cloned and archives are stored, and how many archived sessions are kept.' },
  { id: 'appearance',    group: 'workspace', label: 'Appearance',          icon: Palette,           scope: 'browser', component: AppearanceSection, ownsChassis: true,
    description: 'Theme, navigation and layout for this browser only.', keywords: ['theme', 'dark', 'light'] },
  // Notifications, Channels and Routing rules were one section until #2077: two
  // CRUD lists under two forms is sibling sections, not one column (PAGE-TABS §2).
  // Only push is per-device; channels and rules are server state.
  { id: 'notifications', group: 'workspace', label: 'Notifications',       icon: Bell,              scope: 'device',  component: NotificationsSection, ownsChassis: true,
    description: 'Push to this device, and the quiet hours when only critical notifications get through.', keywords: ['push', 'quiet hours'] },
  { id: 'channels',      group: 'workspace', label: 'Channels',            icon: Send,              scope: 'server',  component: NotificationChannelsSection, ownsChassis: true,
    description: 'Webhook endpoints a routing rule can send a notification to.', keywords: ['webhook', 'slack', 'notifications'] },
  { id: 'rules',         group: 'workspace', label: 'Routing rules',       icon: Route,             scope: 'server',  component: NotificationRulesSection, ownsChassis: true,
    description: 'Which events notify, and whether they push, go to a channel or are ignored.', keywords: ['notifications', 'routing', 'critical'] },
  { id: 'sessions',      group: 'sessions',  label: 'Defaults',            icon: Play,              scope: 'server',  component: SessionDefaultsSection, ownsChassis: true,
    // Names the three groups AND keeps the section's own "these are defaults, not
    // locks" caveat, which the registry sentence had dropped.
    description: 'What every new session starts with — tool approvals, model gating and transcript display — and a session can change any of them for itself once it is running.', keywords: ['permission', 'model', 'transcript'] },
  { id: 'presets',       group: 'sessions',  label: 'Presets',             icon: Bookmark,          scope: 'server',  component: PresetsSection,
    description: 'Saved launch configurations for the New session dialog.' },
  { id: 'templates',     group: 'sessions',  label: 'Templates',           icon: FileText,          scope: 'server',  component: TemplatesSection,
    description: 'Reusable prompt templates with {{variable}} placeholders.' },
  { id: 'profiles',      group: 'sessions',  label: 'Permission profiles', icon: ShieldCheck,       scope: 'server',  component: PermissionProfilesSection,
    description: 'Named tool-permission sets a session can adopt.' },
  // What the fleet can run (spec 2026-09-20 D1/D2): supply, not spend. Delivery →
  // Capacity is the other half — how automation spends it — and the two link.
  { id: 'capacity',      group: 'capacity',  label: 'Limits',              icon: Gauge,             scope: 'server',  component: LimitsSection, ownsChassis: true,
    description: 'The global cap as its two ceilings — what the hosts can run and how much you want running — plus the env-managed limits outside the stack.', keywords: ['global cap', 'attention', 'physical', 'max active sessions'] },
  { id: 'fleet',         group: 'capacity',  label: 'Hosts',               icon: Server,            scope: 'server',  component: FleetSection, readout: 'hosts',
    description: 'Machines that can run sessions: each host’s cap, dispatch weight and health.' },
  { id: 'accounts',      group: 'capacity',  label: 'Accounts',            icon: Users,             scope: 'server',  component: AccountsManager, badge: 'accountsAttention', readout: 'accounts',
    description: 'Claude Max and Copilot accounts the rotation can pick from.' },
  { id: 'plugins',       group: 'claude',    label: 'Plugins',             icon: Puzzle,            scope: 'server',  component: PluginsSection,
    description: 'Installed Claude Code plugins and what each one adds.' },
  { id: 'skills',        group: 'claude',    label: 'Skills',              icon: Sparkles,          scope: 'server',  component: SkillsSection,
    description: 'Skills available to every session, with an AI draft helper.' },
  { id: 'commands',      group: 'claude',    label: 'Commands',            icon: SquareTerminal,    scope: 'server',  component: CommandsSection,
    description: 'Slash commands available to every session.' },
  { id: 'mcp',           group: 'claude',    label: 'MCP servers',         icon: Plug,              scope: 'server',  component: McpSection,
    description: 'Model Context Protocol servers sessions can call, with OAuth where needed.' },
  { id: 'hooks',         group: 'claude',    label: 'Hooks',               icon: Webhook,           scope: 'server',  component: HooksSection,
    description: 'Shell commands Claude Code runs on lifecycle events. They run on this host.' },
  { id: 'snapshots',     group: 'claude',    label: 'Snapshots',           icon: Camera,            scope: 'server',  component: SnapshotsSection,
    description: 'Point-in-time copies of the shared Claude config, with diff, restore, export and import.' },
  // Sits in the About group beside the version it changes. It is also the phone's
  // ONLY deploy surface — `shell/useAppBanners` hides the deploy banner there —
  // so the badge is load-bearing rather than decorative: it is what tells a phone
  // user there is anything here to look at.
  { id: 'deploy',        group: 'about',     label: 'Deploy',              icon: Rocket,            scope: 'server',  component: DeploySection, ownsChassis: true, badge: 'deployPending',
    description: 'Commits waiting to ship to this server, and how the last deploy went.', keywords: ['ship', 'release', 'pending', 'update'] },
  { id: 'about',         group: 'about',     label: 'About',               icon: Info,              scope: 'none',    component: AboutSection, ownsChassis: true,
    description: 'Version, host, and the server settings managed by environment variables.' },
] as const satisfies readonly SettingsDefShape[]

export type SettingsSectionId = (typeof DEFS)[number]['id']
export interface SettingsDef extends SettingsDefShape { id: SettingsSectionId }

export const SETTINGS_DEFS: readonly SettingsDef[] = DEFS
export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = 'general'

export const SETTINGS_BY_ID: Record<SettingsSectionId, SettingsDef> = (() => {
  const out = {} as Record<SettingsSectionId, SettingsDef>
  for (const d of SETTINGS_DEFS) {
    if (d.id in out) throw new Error(`settingsDefs: duplicate id ${d.id}`)
    out[d.id] = d
  }
  return out
})()

export function isSettingsSectionId(x: string | null | undefined): x is SettingsSectionId {
  return typeof x === 'string' && Object.prototype.hasOwnProperty.call(SETTINGS_BY_ID, x)
}

export function sectionsInGroup(group: SettingsGroupId): SettingsDef[] {
  return SETTINGS_DEFS.filter((d) => d.group === group)
}
