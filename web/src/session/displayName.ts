/**
 * A session's display name: its name, or a short id for the unnamed. One rule for every
 * surface that names a session from its record — the archived list and its delete
 * dialogs, the shell's rename/delete targets, and the Assistant drawer's "About:" line
 * (D-12). The open session's own header reads `rt.title`, which adds the live
 * transcript's title between the two.
 */
export function displayNameOf(s: { id: string; name?: string | null }): string {
  return s.name || s.id.slice(0, 8)
}
