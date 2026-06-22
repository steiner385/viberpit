import { OPTIMISTIC_USER_ID, type Turn } from './reducer'

/**
 * Previously-sent prompt texts for a session, oldest → newest. Derived from the
 * transcript the backend replays on attach (so history is persisted "for free"),
 * excluding the transient optimistic-seed turn and any empty-text turns.
 */
export function deriveHistory(turns: Turn[]): string[] {
  const out: string[] = []
  for (const t of turns) {
    if (t.role !== 'user' || t.id === OPTIMISTIC_USER_ID) continue
    for (const b of t.blocks) {
      if (b.kind === 'text') {
        if (b.text) out.push(b.text)
        break
      }
    }
  }
  return out
}
