import { useSettings } from '@/api/hooks'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { ManagedEnvTable } from '../chassis/ManagedEnvTable'

/** Read-only (spec §8 About, S30): what the operator set in the env file, shown so posture can be confirmed from the UI. */
export function AboutSection() {
  const { data: settings, isError, error } = useSettings()
  return (
    <SettingsSection id="about" title="About" scope="none"
      description={SETTINGS_BY_ID.about.description}>
      {/* `!settings`, not `isLoading` (R17): a FAILED request leaves `isLoading`
          false with no data, so the skeleton used to pulse forever and never say
          why. Data in hand always wins — a failed refetch keeps the table. */}
      {!settings ? (
        isError
          ? (
            <Banner tone="danger" role="alert" title="Settings could not be loaded">
              {error?.message ?? 'The server did not answer.'}
            </Banner>
          )
          : <Skeleton lines={4} />
      ) : (
        <>
          <SettingsGroup legend="This server">
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 py-3 text-sm">
              <dt className="text-ink-muted">Version</dt><dd className="font-mono text-ink">{settings.about.gitSha ?? 'unknown'}</dd>
              <dt className="text-ink-muted">Host</dt><dd className="font-mono text-ink">{settings.about.hostName}</dd>
            </dl>
          </SettingsGroup>

          <SettingsGroup legend="Managed by env"
            description={<>Set in <code>/etc/viberpit/viberpit.env</code> and read at daemon start. Not editable here.</>}>
            <ManagedEnvTable flags={settings.managed} />
          </SettingsGroup>
        </>
      )}
    </SettingsSection>
  )
}
