import { useSettings, type ManagedFlag } from '@/api/hooks'
import { Skeleton } from '@/ui/Skeleton'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'

function showValue(v: ManagedFlag['value']): string {
  if (v === null) return 'unset'
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  return String(v)
}

/** Read-only (spec §8 About, S30): what the operator set in the env file, shown so posture can be confirmed from the UI. */
export function AboutSection() {
  const { data: settings, isLoading } = useSettings()
  return (
    <SettingsSection id="about" title="About" scope="none"
      description={SETTINGS_BY_ID.about.description}>
      {isLoading || !settings ? <Skeleton lines={4} /> : (
        <>
          <SettingsGroup legend="This server">
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 py-3 text-sm">
              <dt className="text-ink-muted">Version</dt><dd className="font-mono text-ink">{settings.about.gitSha ?? 'unknown'}</dd>
              <dt className="text-ink-muted">Host</dt><dd className="font-mono text-ink">{settings.about.hostName}</dd>
            </dl>
          </SettingsGroup>

          <SettingsGroup legend="Managed by env"
            description={<>Set in <code>/etc/ccswitch/ccswitch.env</code> and read at daemon start. Not editable here.</>}>
            <div className="overflow-x-auto py-2">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-ink-muted">
                    <th scope="col" className="py-1 pr-4 font-medium">Variable</th>
                    <th scope="col" className="py-1 pr-4 font-medium">Value</th>
                    <th scope="col" className="py-1 font-medium">What it does</th>
                  </tr>
                </thead>
                <tbody>
                  {settings.managed.map((m) => (
                    <tr key={m.key} className="border-t border-edge">
                      <th scope="row" className="py-1.5 pr-4 font-mono text-xs font-normal text-ink">{m.key}</th>
                      <td className="py-1.5 pr-4 font-mono text-xs">{showValue(m.value)}</td>
                      <td className="py-1.5 text-ink-muted">{m.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SettingsGroup>
        </>
      )}
    </SettingsSection>
  )
}
