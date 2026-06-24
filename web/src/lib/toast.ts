export type ToastVariant = 'info' | 'success' | 'error'

export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastItem {
  id: number
  message: string
  variant: ToastVariant
  action?: ToastAction
}

const DEFAULT_MS = 5000
const ACTION_MS = 8000

let toasts: ToastItem[] = []
let nextId = 1
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// Stable reference between mutations — required by useSyncExternalStore.
export function getToasts(): ToastItem[] {
  return toasts
}

export function dismissToast(id: number): void {
  const next = toasts.filter((t) => t.id !== id)
  if (next.length !== toasts.length) {
    toasts = next
    emit()
  }
}

export function toast(input: {
  message: string
  variant?: ToastVariant
  action?: ToastAction
  durationMs?: number
}): number {
  const id = nextId++
  toasts = [...toasts, { id, message: input.message, variant: input.variant ?? 'info', action: input.action }]
  emit()
  const ms = input.durationMs ?? (input.action ? ACTION_MS : DEFAULT_MS)
  if (ms > 0 && typeof setTimeout !== 'undefined') {
    setTimeout(() => dismissToast(id), ms)
  }
  return id
}

/** Test-only: clear all state between tests. */
export function __resetToasts(): void {
  toasts = []
  nextId = 1
  listeners.clear()
}
