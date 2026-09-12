import { storageKey } from '@/lib/storageKey'

/**
 * The persisted theme choice's key. Lives beside `theme.tsx` rather than in it:
 * that file exports the provider, and `react-refresh/only-export-components`
 * allows only literal constants alongside a component — a `storageKey(...)`
 * call is not one, and a non-literal export really does break Fast Refresh
 * for the provider file.
 */
export const THEME_STORAGE_KEY = storageKey('theme')
