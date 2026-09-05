import { isSettingsSectionId, type SettingsSectionId } from './settingsDefs'

/** DEC-9: bare /settings on desktop reopens the last section (C1 "restore the most recently viewed pane"). */
export const LAST_SECTION_KEY = 'ccswitch.settings.last'

export function readLastSection(): SettingsSectionId | null {
  try {
    const v = localStorage.getItem(LAST_SECTION_KEY)
    return isSettingsSectionId(v) ? v : null
  } catch { return null }
}

export function writeLastSection(id: SettingsSectionId): void {
  try { localStorage.setItem(LAST_SECTION_KEY, id) } catch { /* storage unavailable */ }
}
