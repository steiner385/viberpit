import {
  isOverlayName, useClearOverlayParam, useOverlayParam,
} from './overlayParam'

/**
 * The `?dialog=` contract — the sibling of `drawerParam.ts`, sharing its
 * mechanics through `overlayParam.ts`.
 *
 * ## Why this exists (Task 18, Item 0 — a live regression)
 *
 * `GlobalActions` ships `/sessions?dialog=new` as the rail's primary action, and
 * `NewSessionDialog` is the form that param names. Between the two, the reader
 * was `Dashboard`'s — a `dialog` prop threaded down from `App.tsx`. Task 17
 * deleted both, so the cutover left the param written by the rail, read by
 * nobody, and the New button did nothing in every layout mode. DEC-5 requires it
 * to open the dialog in every mode, so the reader lives here — beside the param
 * name, not inside whichever component happens to host the form today.
 *
 * ## Why there is no `useOpenDialog`
 *
 * Nothing in the app opens this dialog imperatively: the rail renders a `<Link>`
 * to `/sessions?dialog=new`, which is what makes the action middle-clickable and
 * copyable. A setter here would be an unused second way to do it — exactly the
 * parallel path this module exists to prevent. Add one when a caller needs it.
 *
 * Closing DOES need a hook (a dialog's own Cancel/Escape is not a link), and it
 * `replace`s: opening a dialog is not a place Back should return to (§4.4).
 */
export const DIALOG_PARAM = 'dialog'

/**
 * Every dialog the shell renders from the URL.
 *
 * `settings` is deliberately NOT here. It is a legacy value that
 * `routes.tsx`'s `SettingsDialogRedirect` turns into `/settings/general` above
 * the layout route, so it never reaches a dialog reader — listing it would
 * claim a dialog that does not exist.
 */
export const DIALOG_NAMES = ['new'] as const

export type DialogName = (typeof DIALOG_NAMES)[number]

export function isDialogName(value: string | null | undefined): value is DialogName {
  return isOverlayName(value, DIALOG_NAMES)
}

/**
 * The open dialog, or `null`.
 *
 * An unrecognised value reads as `null`, the same rule `?drawer=` follows: a
 * `?dialog=bogus` from a stale link must render nothing, not an empty modal the
 * user has to dismiss.
 */
export function useDialogName(): DialogName | null {
  return useOverlayParam(DIALOG_PARAM, DIALOG_NAMES)
}

export function useCloseDialog(): () => void {
  return useClearOverlayParam(DIALOG_PARAM)
}
