export const STORAGE_NAMESPACE = 'viberpit'

/**
 * The pre-rebrand namespace (#1368). Every persisted key was `ccswitch.<name>`;
 * readers migrate one forward the first time they read it (see
 * `migrateStorageNamespace`), so the release that flips this keeps every user's
 * theme, list widths, filters and unsent drafts.
 */
export const LEGACY_STORAGE_NAMESPACE = 'ccswitch'

/**
 * Builds a namespaced storage key for UI state.
 * @param name The local name (e.g., 'theme', 'listWidth')
 * @returns The fully namespaced key (e.g., 'viberpit.theme')
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

/**
 * Migrate-on-read across the brand namespace rename (#1368): moves
 * `ccswitch.<name>` forward to `viberpit.<name>` and returns *key*, so a reader
 * spends one line — `storage.getItem(migrateStorageNamespace(storage, KEY))`.
 *
 * NOT a second migration mechanism: it is `migrateStorageKey` with the two
 * namespaces filled in, which is the `notifyPref.ts` pattern (read legacy →
 * write new → remove legacy) applied once per persisted key. Keeping it a
 * *read-time* migration rather than a one-shot sweep at boot matters for the
 * dynamic keys — `draft.<sessionId>`, `groupCollapse.<ns>` — whose names are not
 * enumerable up front.
 *
 * A key outside the namespace is returned untouched, so a caller cannot
 * accidentally invent a `ccswitch.`-prefixed twin for a key that never had one.
 */
export function migrateStorageNamespace(storage: Storage, key: string): string {
  const prefix = `${STORAGE_NAMESPACE}.`
  if (!key.startsWith(prefix)) return key
  const legacy = `${LEGACY_STORAGE_NAMESPACE}.${key.slice(prefix.length)}`
  migrateStorageKey(storage, legacy, key)
  return key
}
