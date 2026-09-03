/**
 * The router-`state` contract for opening a session with a seeded prompt.
 *
 * When a session is created from the composer the user's text is carried to the
 * session route in `navigate(..., { state })` so the transcript can show it
 * immediately, before the SDK echoes it back (`useSessionRuntime`'s
 * `_seedUserPrompt`). It is deliberately NOT a search param: it is one-shot,
 * can be long, and must not survive a refresh or a shared link.
 *
 * The key lived as a bare string literal at the write site and as an inline
 * `as { initialPrompt?: string }` cast at the read site. Two halves of one
 * contract, neither checked against the other — rename one and the prompt
 * silently stops seeding. Both halves live here now, so there is one owner.
 *
 * `readInitialPrompt` narrows from `unknown` without a cast: router state is
 * genuinely arbitrary (a deep link arrives with `state === null`, and a
 * back/forward entry can carry state written by an older build), so it is
 * validated rather than asserted.
 */

/** Router state understood by the session route. */
export interface SessionOpenState {
  initialPrompt?: string
}

/**
 * The `navigate()` options for opening a session, or `undefined` when there is
 * no prompt to seed — `navigate(path, undefined)` is the plain-navigation case,
 * so callers can pass the result through unconditionally.
 */
export function sessionOpenState(initialPrompt?: string): { state: SessionOpenState } | undefined {
  return initialPrompt ? { state: { initialPrompt } } : undefined
}

/** The seeded prompt carried by `useLocation().state`, or `undefined`. */
export function readInitialPrompt(state: unknown): string | undefined {
  if (typeof state !== 'object' || state === null) return undefined
  if (!('initialPrompt' in state)) return undefined
  const value = state.initialPrompt
  return typeof value === 'string' && value !== '' ? value : undefined
}
