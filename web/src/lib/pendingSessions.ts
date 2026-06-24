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

export function addPendingSession(s: Omit<Session, 'id' | 'pending'>): string {
  const id = `pending-${nextTempId++}`
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
