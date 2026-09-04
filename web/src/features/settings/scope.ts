/** Where a setting applies (spec D7). Rendered as a badge on every section. */
export type SettingsScope = 'server' | 'browser' | 'device' | 'none'

export const SCOPE_LABEL: Record<SettingsScope, string> = {
  server: 'Server',
  browser: 'This browser',
  device: 'This device',
  none: 'Read-only',
}
