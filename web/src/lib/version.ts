// The bundle's build sha, injected by vite.config.ts `define`. Read through a typeof guard so a
// test/build environment that didn't apply the define resolves to 'dev' instead of throwing a
// ReferenceError. Single source of truth for reading the global (constitution VI).
export const BUILD_SHA: string = typeof __BUILD_SHA__ !== 'undefined' ? __BUILD_SHA__ : 'dev'

// Statuses where the focused session is actively mid-turn — reloading would interrupt a
// stream the user is watching. (Other statuses are terminal/idle and safe to reload through.)
const UNSAFE_STATUSES = new Set(['in_progress', 'waiting_input', 'waiting_question'])

/** True when the loaded bundle's sha differs from the server's running sha. Conservative:
 *  returns false when the server sha is unknown, or the build is the dev placeholder. */
export function isOutdated(buildSha: string, serverSha: string | null | undefined): boolean {
  if (!serverSha) return false
  if (buildSha === 'dev') return false
  return buildSha !== serverSha
}

/** True when an auto-reload would not lose work or interrupt a watched stream:
 *  no client-buffered prompts, and the focused session (if any) is not mid-turn. */
export function isSafeToReload(focusedStatus: string | undefined, bufferedCount: number): boolean {
  if (bufferedCount > 0) return false
  if (focusedStatus && UNSAFE_STATUSES.has(focusedStatus)) return false
  return true
}
