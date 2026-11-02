/**
 * The Changes tab's remembered Unified/Split choice (D-4), per browser.
 *
 * Same shape as `lib/enginePreference.ts`: null until the user has picked one,
 * so the caller can tell "never chose" from a real choice and fall back to the
 * viewport default (unified below 1024px, split from 1024px). Every access is
 * wrapped so a blocked localStorage degrades to "no remembered choice".
 */
import type { DiffViewMode } from './DiffRenderer'
import { storageKey, migrateStorageNamespace } from '@/lib/storageKey'

export const DIFF_VIEW_KEY = storageKey('diffView')

export function getDiffViewPreference(): DiffViewMode | null {
  try {
    const raw = window.localStorage.getItem(migrateStorageNamespace(window.localStorage, DIFF_VIEW_KEY))
    return raw === 'unified' || raw === 'split' ? raw : null
  } catch {
    return null
  }
}

export function setDiffViewPreference(mode: DiffViewMode): void {
  try {
    window.localStorage.setItem(DIFF_VIEW_KEY, mode)
  } catch {
    /* ignore — persistence is best-effort */
  }
}
