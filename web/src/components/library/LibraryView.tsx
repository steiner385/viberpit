/**
 * Library — manage plugins, skills, slash commands, and MCP servers.
 * These live in the SHARED config base (mirrored into every account), so
 * everything here applies to all accounts; changes take effect for newly
 * started sessions.
 */
import { useSearchParams } from 'react-router-dom'
import { PluginsSection } from './PluginsSection'
import { SkillsSection } from './SkillsSection'
import { CommandsSection } from './CommandsSection'
import { McpSection } from './McpSection'

export type LibrarySection = 'plugins' | 'skills' | 'commands' | 'mcp'

const SECTIONS: { id: LibrarySection; label: string }[] = [
  { id: 'plugins', label: 'Plugins' },
  { id: 'skills', label: 'Skills' },
  { id: 'commands', label: 'Commands' },
  { id: 'mcp', label: 'MCP Servers' },
]

export function LibraryView() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('section')
  const section: LibrarySection = SECTIONS.some((s) => s.id === raw) ? (raw as LibrarySection) : 'plugins'

  return (
    <div className="max-w-3xl mx-auto w-full">
      <div role="tablist" aria-label="Library sections" className="flex gap-1 mb-4 border-b border-[var(--border)]">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            className={`px-3 py-2 text-sm rounded-t-md min-h-[44px] ${
              section === s.id
                ? 'font-semibold border-b-2 border-current'
                : 'opacity-70 hover:opacity-100'
            }`}
            onClick={() => setParams((p) => { p.set('section', s.id); return p }, { replace: true })}
          >
            {s.label}
          </button>
        ))}
      </div>
      {section === 'plugins' && <PluginsSection />}
      {section === 'skills' && <SkillsSection />}
      {section === 'commands' && <CommandsSection />}
      {section === 'mcp' && <McpSection />}
    </div>
  )
}
