import type * as React from 'react'
import {
  SlidersHorizontal, Palette, Bell, Play, Bookmark, FileText, ShieldCheck, Workflow,
  Users, Server, Puzzle, Sparkles, SquareTerminal, Plug, Webhook, Camera, Info,
  type LucideIcon,
} from 'lucide-react'
import type { SettingsScope } from './scope'
import { GeneralSection } from './sections/GeneralSection'
import { AppearanceSection } from './sections/AppearanceSection'
import { SessionDefaultsSection } from './sections/SessionDefaultsSection'
import { AboutSection } from './sections/AboutSection'
import { AccountsManager } from '@/components/AccountsManager'
import { NotificationsSection } from '@/components/settings/NotificationsSection'
import { HooksSection } from '@/components/settings/HooksSection'
import { SnapshotsSection } from '@/components/settings/SnapshotsSection'
import { FleetSection } from '@/components/settings/FleetSection'
import { PresetsSection } from '@/components/settings/PresetsSection'
import { TemplatesSection } from '@/components/settings/TemplatesSection'
import { PermissionProfilesSection } from '@/components/settings/PermissionProfilesSection'
import { AutomationView } from '@/components/AutomationView'
import { PluginsSection } from '@/components/library/PluginsSection'
import { SkillsSection } from '@/components/library/SkillsSection'
import { CommandsSection } from '@/components/library/CommandsSection'
import { McpSection } from '@/components/library/McpSection'

export type SettingsGroupId = 'workspace' | 'sessions' | 'accounts' | 'claude' | 'about'
export interface SettingsGroupDef { id: SettingsGroupId; label: string }

/** Nav order. Frequency-first (spec §5): weekly changes above yearly ones. */
export const SETTINGS_GROUPS: readonly SettingsGroupDef[] = [
  { id: 'workspace', label: 'Workspace' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'claude', label: 'Claude config' },
  { id: 'about', label: 'About' },
]

export type SettingsBadgeKey = 'accountsAttention'

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
  component: React.ComponentType
}

/**
 * The one Settings map (spec §6.2). SettingsNav, the phone list, the route
 * fallback and pageContract.test.tsx all derive from this array. Ids are URL
 * segments and are stable: every id that existed before this file did is kept.
 */
const DEFS = [
  { id: 'general',       group: 'workspace', label: 'General',             icon: SlidersHorizontal, scope: 'server',  component: GeneralSection,
    description: 'Where repos are cloned and archives are stored, and how many sessions run and are kept.' },
  { id: 'appearance',    group: 'workspace', label: 'Appearance',          icon: Palette,           scope: 'browser', component: AppearanceSection,
    description: 'Theme, navigation and layout for this browser only.', keywords: ['theme', 'dark', 'light'] },
  { id: 'notifications', group: 'workspace', label: 'Notifications',       icon: Bell,              scope: 'device',  component: NotificationsSection,
    description: 'Push to this device, quiet hours, webhook channels and routing rules.' },
  { id: 'sessions',      group: 'sessions',  label: 'Defaults',            icon: Play,              scope: 'server',  component: SessionDefaultsSection,
    description: 'What every new session starts with: tool approvals, model gating and transcript display.', keywords: ['permission', 'model', 'transcript'] },
  { id: 'presets',       group: 'sessions',  label: 'Presets',             icon: Bookmark,          scope: 'server',  component: PresetsSection,
    description: 'Saved launch configurations for the New session dialog.' },
  { id: 'templates',     group: 'sessions',  label: 'Templates',           icon: FileText,          scope: 'server',  component: TemplatesSection,
    description: 'Reusable prompt templates with {{variable}} placeholders.' },
  { id: 'profiles',      group: 'sessions',  label: 'Permission profiles', icon: ShieldCheck,       scope: 'server',  component: PermissionProfilesSection,
    description: 'Named tool-permission sets a session can adopt.' },
  // R10: Automations keeps a Settings section until PR 6. The top-level
  // `/automations` page is a MONITOR — events, spawned sessions, manual
  // dispatch — and has no enable switch, mode select, config form or schedule
  // editor. Those live only here, so this is the one place an automation can be
  // configured. PR 6 folds them into the page and drops this row.
  { id: 'automation',    group: 'sessions',  label: 'Automations',         icon: Workflow,          scope: 'server',  component: AutomationView,
    description: 'Enable, configure and schedule the automations that run against your repos and sessions.' },
  { id: 'accounts',      group: 'accounts',  label: 'Accounts',            icon: Users,             scope: 'server',  component: AccountsManager, badge: 'accountsAttention',
    description: 'Claude Max and Copilot accounts the rotation can pick from.' },
  { id: 'fleet',         group: 'accounts',  label: 'Hosts',               icon: Server,            scope: 'server',  component: FleetSection,
    description: 'Machines that can run sessions, their weight and capacity.' },
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
  { id: 'about',         group: 'about',     label: 'About',               icon: Info,              scope: 'none',    component: AboutSection,
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
