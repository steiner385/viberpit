/* eslint-disable react-refresh/only-export-components -- `useSettingsDrill` is
   the one answer to "does this viewport drill", read by BOTH halves of the
   list-then-detail pair. Splitting it into its own module to satisfy fast
   refresh would separate the predicate from the page whose layout it decides,
   and give the two pages two places to disagree — the same convention, and the
   same reason, as shell/routes.tsx and shell/useViewport.tsx. */
import * as React from 'react'
import { Navigate, NavLink, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { useViewport } from '@/shell/useViewport'
import { NAV_BY_ID, settingsSectionPath } from '@/shell/navDefs'
import { SETTINGS_BY_ID, DEFAULT_SETTINGS_SECTION, isSettingsSectionId, type SettingsSectionId } from './settingsDefs'
import { writeLastSection } from './lastSection'
import { SettingsNav } from './SettingsNav'
import { HostedSection } from './HostedSection'

/** Phone, or tablet held portrait, gets list-then-detail (spec §6.1, DEC-3). */
export function useSettingsDrill(): boolean {
  const { mode, portrait } = useViewport()
  return mode === 'phone' || (mode === 'tablet' && portrait)
}

/**
 * A section that renders its own `SettingsSection` is mounted BARE; everything
 * else is wrapped (spec §8, DEC-12). The answer lives on the registry row
 * (`ownsChassis`), not in a second set here: a set beside the page is one more
 * place to forget when PRs 5b–5e migrate a section, and forgetting it means two
 * nested h2s with no test naming the page as the culprit.
 */
function SectionBody({ id }: { id: SettingsSectionId }) {
  const def = SETTINGS_BY_ID[id]
  if (def.ownsChassis) {
    const Section = def.component
    return <Section />
  }
  return <HostedSection def={def} />
}

/**
 * `/settings/:section` — the Split variant page (umbrella D11).
 *
 * ## What replaced what
 *
 * `SettingsView` put fourteen sections in one horizontal `Tabs` strip that
 * overflowed at every width and had its own hand-written label list. Here the
 * nav is derived from `settingsDefs`, grouped, and vertical — and on phone it is
 * not on this page at all: `SettingsIndexPage` is the list and this is the
 * detail (`useSettingsDrill`).
 *
 * ## The one redirect
 *
 * An unknown `:section` is a stale bookmark and lands on the default section
 * rather than on an empty panel. It REPLACES: pushed, it would leave the bad URL
 * in the history stack for Back to bounce off forever.
 *
 * `automation` is NOT special-cased here. DEC-10 said it had moved to the
 * top-level page; R10 reversed that, because `/automations` monitors
 * automations and cannot configure them — see the row in `settingsDefs`.
 */
export function SettingsPage() {
  const { section } = useParams()
  const drill = useSettingsDrill()

  // BEFORE the redirects below, not after: an effect placed after an early
  // return runs on some renders and not others, which is the rules-of-hooks
  // violation React cannot recover from. The guard moves inside instead — a
  // section that is about to be redirected away is not one to remember.
  React.useEffect(() => {
    if (isSettingsSectionId(section)) writeLastSection(section)
  }, [section])

  if (!isSettingsSectionId(section)) return <Navigate to={settingsSectionPath(DEFAULT_SETTINGS_SECTION)} replace />

  const def = SETTINGS_BY_ID[section]

  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="split">
      <PageHeader
        title="Settings"
        // The h1 stays "Settings" — one page, one name (WCAG 2.4.2 wants the
        // TITLE to disambiguate, not the heading to change under the user).
        documentTitle={`${def.label} · Settings`}
        meta={def.label}
        actions={drill ? (
          <NavLink
            to={NAV_BY_ID.settings.path}
            aria-label="Back to Settings"
            className="inline-flex items-center gap-1 text-sm text-accent min-h-target"
          >
            <ArrowLeft size={16} aria-hidden="true" /> Settings
          </NavLink>
        ) : undefined}
      />
      <div className="flex min-h-0 flex-1">
        {!drill && <SettingsNav variant="rail" />}
        {/* scroll-pb-20 (5rem): the SaveBar docks at the bottom of a form
            section, so an anchored/focused control must not land under it. */}
        <PageContent className="scroll-pb-20">
          <SectionBody id={section} />
        </PageContent>
      </div>
    </div>
  )
}
