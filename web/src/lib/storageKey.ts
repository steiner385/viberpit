export const STORAGE_NAMESPACE = 'ccswitch'

/**
 * Builds a namespaced storage key for UI state.
 * @param name The local name (e.g., 'theme', 'listWidth')
 * @returns The fully namespaced key (e.g., 'ccswitch.theme')
 */
export function storageKey(name: string): string {
  return `${STORAGE_NAMESPACE}.${name}`
}

/**
 * Migrates a value from an old storage key to a new one.
 *
 * If the old key has a value and the new key is absent, copies it to the new key.
 * Always removes the old key afterward. Swallows exceptions (private mode, quota errors).
 *
 * @param storage The Storage object to use (usually window.localStorage)
 * @param from The old key
 * @param to The new key
 */
export function migrateStorageKey(storage: Storage, from: string, to: string): void {
  try {
    const oldValue = storage.getItem(from)
    // Nothing to migrate: return before touching storage. Callers run this on
    // every read (some from a useSyncExternalStore getSnapshot), so the
    // steady-state path must be one read and no write.
    if (oldValue === null) return

    // Copy old → new only if new is absent; always remove the old key.
    if (storage.getItem(to) === null) storage.setItem(to, oldValue)
    storage.removeItem(from)
  } catch {
    // Swallow exceptions: storage may be unavailable (private mode, quota exceeded, etc.)
  }
}
