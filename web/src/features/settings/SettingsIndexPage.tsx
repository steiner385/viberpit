import { Navigate, useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { settingsSectionPath } from '@/shell/navDefs'
import { DEFAULT_SETTINGS_SECTION } from './settingsDefs'
import { readLastSection } from './lastSection'
import { SettingsNav } from './SettingsNav'
import { useSettingsDrill } from './SettingsPage'

/**
 * Bare `/settings`: the grouped list when drilling, else a redirect (DEC-9).
 *
 * `?section=` is checked FIRST and unconditionally. It is the modal-era deep
 * link and it names a section explicitly, so it must win over both the
 * drill-down list (which would swallow it) and the remembered section (which
 * would send the user somewhere the link did not ask for).
 */
export function SettingsIndexPage() {
  const [params] = useSearchParams()
  const drill = useSettingsDrill()

  const legacy = params.get('section')
  if (legacy) return <Navigate to={settingsSectionPath(legacy)} replace />
  // Not drilling, there is no list to show: the nav rail lives beside a section,
  // so a bare /settings would be a rail next to nothing. C1's "restore the most
  // recently viewed pane" picks which section that is.
  if (!drill) return <Navigate to={settingsSectionPath(readLastSection() ?? DEFAULT_SETTINGS_SECTION)} replace />

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="split">
      <PageHeader title="Settings" />
      <PageContent>
        <SettingsNav variant="list" />
      </PageContent>
    </div>
  )
}
