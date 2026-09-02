/**
 * SettingsView — the consolidated Settings tab: app settings plus the
 * accounts, automation, and library (plugins/skills/commands/MCP) managers.
 * Section state lives in ?section= so deep links work in both shells.
 * Library section ids (plugins|skills|commands|mcp) are preserved so old
 * /library?section=X links redirect losslessly.
 */
import { useSearchParams } from 'react-router-dom'
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

export function SettingsView() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('section')
  const section: SettingsSection = SECTIONS.some((s) => s.id === raw) ? (raw as SettingsSection) : 'general'

  return (
    <div className="max-w-3xl mx-auto w-full">
      <div role="tablist" aria-label="Settings sections" className="flex gap-1 mb-4 border-b border-edge overflow-x-auto">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            className={`px-3 py-2 text-sm rounded-t-md min-h-[44px] whitespace-nowrap ${
              section === s.id ? 'font-semibold border-b-2 border-current' : 'opacity-70 hover:opacity-100'
            }`}
            onClick={() => setParams((p) => { p.set('section', s.id); return p }, { replace: true })}
          >
            {s.label}
          </button>
        ))}
      </div>
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
