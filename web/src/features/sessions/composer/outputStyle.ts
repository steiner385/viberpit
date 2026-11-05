/**
 * Output style (D-16a): the session composer's `+` menu "Style ›" submenu. It lists
 * the styles the session's CLI reported (`availableOutputStyles`, in CLI order) with
 * the current one checked, and choosing one posts `POST /sessions/{id}/output-style`
 * through `useSetOutputStyle`.
 *
 * Hidden — no entry at all — when the list is empty (Codex, the assistant drawer, a
 * CLI that has not reported yet) or the engine is not Claude. The dispatch bar's `+`
 * menu never offers it: a new session starts on the Settings default.
 *
 * `/output-style` needs nothing here: it is a server `control` command, so a typed
 * `/output-style <name>` runs the same set-and-validate action as this submenu and a
 * bare one answers with the list as a notice.
 */
import { useSessions, useSetOutputStyle } from '@/api/hooks'
import type { PlusMenuEntry } from './PlusMenu'

/** The live change misses the prompt cache once; the picker's tooltip says so. */
export const OUTPUT_STYLE_COPY = 'Changing style mid-session re-sends the system prompt on the next turn.'

export function useOutputStyleEntry(sessionId: string): PlusMenuEntry | null {
  const { data: sessions } = useSessions()
  const { mutate } = useSetOutputStyle()
  const session = sessions?.find((s) => s.id === sessionId)
  const styles = session?.availableOutputStyles ?? []
  if (session?.engine !== 'claude_code' || styles.length === 0) return null
  const current = session.outputStyle ?? null
  return {
    id: 'style',
    label: 'Style',
    title: OUTPUT_STYLE_COPY,
    submenu: {
      label: 'Style',
      items: styles.map((name) => ({ id: name, label: name, selected: name === current, title: OUTPUT_STYLE_COPY })),
      onSelect: (name) => { if (name !== current) mutate({ id: sessionId, style: name }) },
    },
  }
}
