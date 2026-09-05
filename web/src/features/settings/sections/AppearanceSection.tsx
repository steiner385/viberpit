import * as React from 'react'
import { useTheme, type ThemeChoice } from '@/shell/theme'
import { useRailExpanded } from '@/shell/railExpanded'
import { useNotifyPref } from '@/pipeline/notifyPref'
import { SIDEBAR_DEFAULT } from '@/lib/sidebarWidth'
import { useSidebarWidth } from '@/lib/useSidebarWidth'
import { DRAWER_DEFAULT, useDrawerWidth } from '@/shell/drawerWidth'
import { RadioGroup } from '@/ui/RadioGroup'
import { Switch } from '@/ui/Switch'
import { Button } from '@/ui/Button'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { SettingRow } from '../chassis/SettingRow'

/** "Saved" for a few seconds after an immediate row applies; `null` shows nothing. */
function useFlash(): [string | null, () => void] {
  const [at, setAt] = React.useState<number | null>(null)
  React.useEffect(() => {
    if (at === null) return
    const t = setTimeout(() => setAt(null), 4000)
    return () => clearTimeout(t)
  }, [at])
  return [at === null ? null : 'Saved', () => setAt(Date.now())]
}

const THEME_OPTIONS: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'Use system setting' },   // C2 wording
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

/**
 * Settings › Appearance (spec §7.2): theme, navigation labels, pipeline
 * notifications and the two resizable layout widths.
 *
 * Every row here is IMMEDIATE and there is deliberately no SaveBar: each one
 * owns a browser-scoped store (`localStorage`, read through the same hook the
 * shell reads, DEC-6) that has already applied by the time the control has
 * finished animating. A Save button over that would be a lie — nothing is
 * pending — so the feedback is the row's own live region flashing "Saved".
 *
 * R14b: the two group-shaped rows (the theme radios, and a width value beside
 * its Reset) pass `selfLabelled`. A `SettingRow`'s `htmlFor` can only name one
 * labelable control, so on those rows a `<label htmlFor>` would point at an id
 * no element carries. The theme row therefore has no row label at all and lets
 * `RadioGroup`'s visible legend be its label — exactly one visible label per
 * row, and it is the one the radios are actually grouped by.
 */
export function AppearanceSection() {
  const { choice, setChoice } = useTheme()
  const [railExpanded, setRailExpanded] = useRailExpanded()
  const [notify, setNotify] = useNotifyPref()
  const [themeStatus, flashTheme] = useFlash()
  const [railStatus, flashRail] = useFlash()
  const [notifyStatus, flashNotify] = useFlash()
  // The widths are set by dragging the shell's dividers, so this section only
  // reports and resets them — through the SAME store the shell holds (R15), so a
  // Reset moves the live column and a drag updates the number here.
  const [listWidth, setListWidth] = useSidebarWidth()
  const [drawerWidth, setDrawerWidth] = useDrawerWidth()

  return (
    <SettingsSection
      id="appearance"
      title="Appearance"
      scope="browser"
      description={SETTINGS_BY_ID.appearance.description}
    >
      {/* The card's legend is not a second "Theme": the row's own label is the
          radio group's legend, and one visible word per row is the rule (R14b). */}
      <SettingsGroup legend="Colour scheme">
        <SettingRow
          htmlFor="appearance-theme"
          wide
          status={themeStatus}
          description="Also in the rail's theme menu; both change the same setting."
          selfLabelled
        >
          <RadioGroup
            name="appearance-theme"
            legend="Theme"
            value={choice}
            onChange={(v) => { setChoice(v); flashTheme() }}
            options={THEME_OPTIONS}
          />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Navigation">
        <SettingRow
          label="Show navigation labels"
          htmlFor="appearance-rail"
          status={railStatus}
          description="Expands the rail on wide screens so each section shows its name."
        >
          <Switch
            id="appearance-rail"
            checked={railExpanded}
            onChange={(v) => { setRailExpanded(v); flashRail() }}
            aria-label="Show navigation labels"
          />
        </SettingRow>
        <SettingRow
          label="Pipeline notifications"
          htmlFor="appearance-notify"
          status={notifyStatus}
          description="Browser alerts when a pull request's status changes on the Pipeline page."
        >
          <Switch
            id="appearance-notify"
            checked={notify}
            onChange={(v) => { setNotify(v); flashNotify() }}
            aria-label="Pipeline notifications"
          />
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup legend="Layout" description="Set by dragging the dividers; reset here.">
        <SettingRow
          label="Session list width"
          htmlFor="appearance-list-width"
          description="The Sessions list column on desktop."
          selfLabelled
        >
          <span className="flex items-center gap-2">
            <span className="text-sm text-ink-muted tabular-nums">{listWidth} px</span>
            <Button
              type="button"
              variant="ghost"
              aria-label="Reset session list width"
              disabled={listWidth === SIDEBAR_DEFAULT}
              onClick={() => setListWidth(SIDEBAR_DEFAULT)}
            >
              Reset
            </Button>
          </span>
        </SettingRow>
        <SettingRow
          label="Assistant drawer width"
          htmlFor="appearance-drawer-width"
          description="The right-hand drawer on desktop."
          selfLabelled
        >
          <span className="flex items-center gap-2">
            <span className="text-sm text-ink-muted tabular-nums">{drawerWidth} px</span>
            <Button
              type="button"
              variant="ghost"
              aria-label="Reset assistant drawer width"
              disabled={drawerWidth === DRAWER_DEFAULT}
              onClick={() => setDrawerWidth(DRAWER_DEFAULT)}
            >
              Reset
            </Button>
          </span>
        </SettingRow>
      </SettingsGroup>
    </SettingsSection>
  )
}
