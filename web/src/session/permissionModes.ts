/**
 * Canonical list of permission modes shared across the New Session dialog,
 * the prompt row and the Settings panel. Single source of truth — do not
 * duplicate. Mode KEYS are the wire contract; only labels and grouping here
 * follow Claude Desktop's Code mode (`desktop`), with ViberPit's own extras
 * under their own heading (`viberpit`).
 */
export type PermissionModeGroup = 'desktop' | 'viberpit'

/** Display order matters: the Desktop group first, then ViberPit's. */
export const PERMISSION_MODES = [
  { value: 'interactive', label: 'Ask', group: 'desktop' },
  { value: 'acceptEdits', label: 'Accept edits', group: 'desktop' },
  { value: 'auto', label: 'Auto', group: 'desktop' },
  { value: 'bypassPermissions', label: 'Bypass', group: 'desktop' },
  { value: 'allowlist', label: 'Read-only allowlist', group: 'viberpit' },
  { value: 'full-auto', label: 'Full auto', group: 'viberpit' },
] as const satisfies readonly { value: string; label: string; group: PermissionModeGroup }[]

export type PermissionMode = (typeof PERMISSION_MODES)[number]['value']

/** Plan is switchable on a live session but is not a settable default. */
export type SessionMode = PermissionMode | 'plan'

const PLAN_OPTION = { value: 'plan', label: 'Plan', group: 'desktop' } as const

/** Every mode a session can be switched to: `PERMISSION_MODES` plus Plan, after Accept edits. */
export const SESSION_MODE_OPTIONS: readonly { value: SessionMode; label: string; group: PermissionModeGroup }[] = [
  ...PERMISSION_MODES.slice(0, 2),
  PLAN_OPTION,
  ...PERMISSION_MODES.slice(2),
]

/** The SDK reports `default` when plan mode exits to the base mode; it reads as Ask. */
const SDK_MODE_ALIASES: Record<string, string> = { default: 'interactive' }

export const PERMISSION_GROUP_HEADINGS: Record<PermissionModeGroup, string | null> = {
  desktop: null,
  viberpit: 'ViberPit',
}

/** Human label for a mode key; an unknown key is shown as-is rather than hidden. */
export function modeLabel(mode: string): string {
  const key = SDK_MODE_ALIASES[mode] ?? mode
  return SESSION_MODE_OPTIONS.find((o) => o.value === key)?.label ?? mode
}
