/**
 * Canonical list of permission modes shared across the New Session dialog
 * and the Settings panel. Single source of truth — do not duplicate.
 */
export const PERMISSION_MODES = [
  { value: 'interactive', label: 'Ask each time' },
  { value: 'auto', label: 'Auto-approve safe tools' },
  { value: 'allowlist', label: 'Allowlist read-only, prompt the rest' },
  { value: 'acceptEdits', label: 'Accept file edits, prompt the rest' },
  { value: 'full-auto', label: 'Full auto — incl. shell ⚠' },
  { value: 'bypassPermissions', label: 'Bypass all permissions — incl. shell ⚠' },
] as const

export type PermissionMode = (typeof PERMISSION_MODES)[number]['value']
