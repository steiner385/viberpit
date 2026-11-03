import type * as React from 'react'
import {
  SlidersHorizontal, Palette, Bell, Play, Bookmark, FileText, ShieldCheck, ListChecks,
  Users, Server, Puzzle, Sparkles, SquareTerminal, Plug, Webhook, Camera, Info,
  Rocket, Gauge, Send, Route,
  type LucideIcon,
} from 'lucide-react'
import type { SettingsScope } from './scope'
import { SETTINGS_META, type SettingsGroupId, type SettingsMeta, type SettingsSectionId } from './settingsMeta'
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
import { PermissionRulesSection } from './sections/PermissionRulesSection'
import { PluginsSection } from './sections/PluginsSection'
import { SkillsSection } from './sections/SkillsSection'
import { CommandsSection } from './sections/CommandsSection'
import { McpSection } from './sections/McpSection'

export type SettingsBadgeKey = 'accountsAttention' | 'deployPending'

/** What the Settings PAGE needs beyond the metadata (`settingsMeta.ts`). */
interface SettingsPageFields {
  /** The nav icon — a page field, not metadata: the palette draws none. */
  icon: LucideIcon
  scope: SettingsScope
  badge?: SettingsBadgeKey
  /** Mount the live capacity readout after the body, with this stack row marked current (spec 2026-09-20 D10). */
  readout?: 'hosts' | 'accounts'
  /**
   * `wide`: the body is a table, list or log, so it takes the full content
   * column. Omitted: a form, held to a readable measure (`max-w-3xl`) so a
   * label never sits a screen-width away from its control.
   */
  layout?: 'wide'
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
 * The page half of each section, keyed by the metadata's id. A `Record` over
 * `SettingsSectionId`, so a section added to `settingsMeta` without a component
 * here — or a component here for an id the metadata lacks — does not compile.
 */
const PAGE: Record<SettingsSectionId, SettingsPageFields> = {
  general:       { icon: SlidersHorizontal, scope: 'server',  component: GeneralSection, ownsChassis: true },
  appearance:    { icon: Palette, scope: 'browser', component: AppearanceSection, ownsChassis: true },
  notifications: { icon: Bell, scope: 'device',  component: NotificationsSection, ownsChassis: true },
  channels:      { icon: Send, scope: 'server',  component: NotificationChannelsSection, ownsChassis: true },
  rules:         { icon: Route, scope: 'server',  component: NotificationRulesSection, ownsChassis: true },
  sessions:      { icon: Play, scope: 'server',  component: SessionDefaultsSection, ownsChassis: true },
  presets:       { icon: Bookmark, scope: 'server',  component: PresetsSection, layout: 'wide' },
  templates:     { icon: FileText, scope: 'server',  component: TemplatesSection, layout: 'wide' },
  profiles:      { icon: ShieldCheck, scope: 'server',  component: PermissionProfilesSection, layout: 'wide' },
  'always-allow': { icon: ListChecks, scope: 'server', component: PermissionRulesSection, layout: 'wide' },
  capacity:      { icon: Gauge, scope: 'server',  component: LimitsSection, ownsChassis: true },
  fleet:         { icon: Server, scope: 'server',  component: FleetSection, layout: 'wide', readout: 'hosts' },
  accounts:      { icon: Users, scope: 'server',  component: AccountsManager, layout: 'wide', badge: 'accountsAttention', readout: 'accounts' },
  plugins:       { icon: Puzzle, scope: 'server',  component: PluginsSection, layout: 'wide' },
  skills:        { icon: Sparkles, scope: 'server',  component: SkillsSection, layout: 'wide' },
  commands:      { icon: SquareTerminal, scope: 'server',  component: CommandsSection, layout: 'wide' },
  mcp:           { icon: Plug, scope: 'server',  component: McpSection, layout: 'wide' },
  hooks:         { icon: Webhook, scope: 'server',  component: HooksSection, layout: 'wide' },
  snapshots:     { icon: Camera, scope: 'server',  component: SnapshotsSection, layout: 'wide' },
  // The phone's ONLY deploy surface (`shell/useAppBanners` hides the deploy
  // banner there), so the badge is load-bearing rather than decorative: it is
  // what tells a phone user there is anything here to look at.
  deploy:        { icon: Rocket, scope: 'server',  component: DeploySection, layout: 'wide', ownsChassis: true, badge: 'deployPending' },
  about:         { icon: Info, scope: 'none',    component: AboutSection, ownsChassis: true },
}

export interface SettingsDef extends SettingsMeta, SettingsPageFields {}

/**
 * The one Settings map (spec §6.2): the metadata in its order, each row joined
 * to its page half. SettingsNav, the phone list, the route fallback and
 * pageContract.test.tsx all derive from this array.
 */
export const SETTINGS_DEFS: readonly SettingsDef[] = SETTINGS_META.map((m) => ({ ...m, ...PAGE[m.id] }))

export const SETTINGS_BY_ID: Record<SettingsSectionId, SettingsDef> = (() => {
  const out = {} as Record<SettingsSectionId, SettingsDef>
  for (const d of SETTINGS_DEFS) out[d.id] = d
  return out
})()

export function sectionsInGroup(group: SettingsGroupId): SettingsDef[] {
  return SETTINGS_DEFS.filter((d) => d.group === group)
}
