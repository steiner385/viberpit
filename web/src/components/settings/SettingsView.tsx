/**
 * SettingsView — the consolidated Settings tab: app settings plus the
 * accounts, automation, and library (plugins/skills/commands/MCP) managers.
 * Section state lives in ?section= so deep links work in both shells.
 * Library section ids (plugins|skills|commands|mcp) are preserved so old
 * /library?section=X links redirect losslessly.
 */
import { useNavigate, useSearchParams } from 'react-router-dom'
import { settingsSectionPath } from '@/shell/navDefs'
import { Tabs } from '@/ui/Tabs'
import { GeneralSection } from './GeneralSection'
import { AccountsManager } from '../AccountsManager'
import { AutomationView } from '../AutomationView'
import { PluginsSection } from '../library/PluginsSection'
import { SkillsSection } from '../library/SkillsSection'
import { CommandsSection } from '../library/CommandsSection'
import { McpSection } from '../library/McpSection'
import { PresetsSection } from './PresetsSection'
import { TemplatesSection } from './TemplatesSection'
import { PermissionProfilesSection } from './PermissionProfilesSection'
import { HooksSection } from './HooksSection'
import { NotificationsSection } from './NotificationsSection'
import { SnapshotsSection } from './SnapshotsSection'
import { FleetSection } from './FleetSection'

export type SettingsSection =
  | 'general' | 'accounts' | 'automation'
  | 'plugins' | 'skills' | 'commands' | 'mcp'
  | 'presets' | 'templates' | 'profiles'
  | 'hooks' | 'notifications' | 'snapshots' | 'fleet'

const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: 'general', label: 'General' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'automation', label: 'Automation' },
  { id: 'notifications', label: 'Notifications' },
  { id: 'hooks', label: 'Hooks' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'skills', label: 'Skills' },
  { id: 'commands', label: 'Commands' },
  { id: 'mcp', label: 'MCP Servers' },
  { id: 'presets', label: 'Presets' },
  { id: 'templates', label: 'Templates' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'snapshots', label: 'Snapshots' },
  { id: 'fleet', label: 'Fleet' },
]

export interface SettingsViewProps {
  /**
   * The section from `/settings/:section` (DEC-14). Takes precedence over the
   * `?section=` search param, which stays supported for links already in the
   * wild — the path is the canonical form, the query is the legacy one.
   *
   * Optional, and `undefined` (not the empty string) means "not routed by path":
   * `useParams()` hands `undefined` when the route declares no `:section`.
   */
  section?: string
}

export function SettingsView({ section: sectionProp }: SettingsViewProps = {}) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const raw = sectionProp ?? params.get('section')
  const section: SettingsSection = SECTIONS.some((s) => s.id === raw) ? (raw as SettingsSection) : 'general'

  return (
    <div className="max-w-3xl mx-auto w-full">
      <Tabs
        items={SECTIONS}
        value={section}
        // The strip writes back through whichever channel the section arrived
        // on. Routed by path, writing `?section=` would be ignored (the prop
        // wins) and the tabs would be inert — the strip has to move the URL that
        // is actually being read.
        onChange={(id) => sectionProp === undefined
          ? setParams((p) => { p.set('section', id); return p }, { replace: true })
          : navigate(settingsSectionPath(id), { replace: true })}
        label="Settings sections"
        className="mb-4 border-b border-edge"
      />
      {section === 'general' && <GeneralSection />}
      {section === 'accounts' && <AccountsManager />}
      {section === 'automation' && <AutomationView />}
      {section === 'notifications' && <NotificationsSection />}
      {section === 'hooks' && <HooksSection />}
      {section === 'plugins' && <PluginsSection />}
      {section === 'skills' && <SkillsSection />}
      {section === 'commands' && <CommandsSection />}
      {section === 'mcp' && <McpSection />}
      {section === 'presets' && <PresetsSection />}
      {section === 'templates' && <TemplatesSection />}
      {section === 'profiles' && <PermissionProfilesSection />}
      {section === 'snapshots' && <SnapshotsSection />}
      {section === 'fleet' && <FleetSection />}
    </div>
  )
}
