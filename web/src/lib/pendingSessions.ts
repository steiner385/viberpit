import type { Session } from '@/api/types'

let pending: Session[] = []
let nextTempId = 1
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function subscribePending(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// Stable reference between mutations — required by useSyncExternalStore.
export function getPendingSessions(): Session[] {
  return pending
}

/**
 * The id prefix an optimistic placeholder carries.
 *
 * It is a CONTRACT, not an implementation detail: a placeholder has no session
 * on the server, so nothing may navigate to it. `Dashboard` and `DesktopShell`
 * each hard-coded `id.startsWith('pending-')` at their own "open a session"
 * entry point; both are deleted, and the prefix now has one owner and one
 * predicate so the next entry point cannot forget the rule (it did — see
 * `isPendingSessionId`'s callers and Task 18's report).
 */
export const PENDING_ID_PREFIX = 'pending-'

/** True for an optimistic placeholder row: it has no server session to open. */
export function isPendingSessionId(id: string): boolean {
  return id.startsWith(PENDING_ID_PREFIX)
}

export function addPendingSession(s: Omit<Session, 'id' | 'pending'>): string {
  const id = `${PENDING_ID_PREFIX}${nextTempId++}`
  pending = [...pending, { ...s, id, pending: true }]
  emit()
  return id
}

export function removePendingSession(id: string): void {
  const next = pending.filter((s) => s.id !== id)
  if (next.length !== pending.length) {
    pending = next
    emit()
  }
}

/** Test-only: clear all state between tests. */
export function __resetPending(): void {
  pending = []
  nextTempId = 1
  listeners.clear()
}
