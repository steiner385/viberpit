import { describe, it, expect, beforeEach } from 'vitest'
import {
  STORAGE_NAMESPACE, LEGACY_STORAGE_NAMESPACE,
  storageKey, migrateStorageKey, migrateStorageNamespace,
} from '../storageKey'

describe('storageKey', () => {
  describe('STORAGE_NAMESPACE', () => {
    it('should be "viberpit"', () => {
      expect(STORAGE_NAMESPACE).toBe('viberpit')
    })

    it('still knows the pre-rebrand namespace, for migrate-on-read (#1368)', () => {
      expect(LEGACY_STORAGE_NAMESPACE).toBe('ccswitch')
    })
  })

  describe('storageKey', () => {
    it('should prefix name with namespace', () => {
      expect(storageKey('theme')).toBe('viberpit.theme')
    })

    it('should handle various names', () => {
      expect(storageKey('listWidth')).toBe('viberpit.listWidth')
      expect(storageKey('pipelineNotifications')).toBe('viberpit.pipelineNotifications')
    })
  })

  describe('migrateStorageKey', () => {
    let storage: Storage

    beforeEach(() => {
      storage = window.localStorage
      storage.clear()
    })

    it('should copy from old key to new key when new key is absent', () => {
      storage.setItem('old.key', 'value123')
      migrateStorageKey(storage, 'old.key', 'new.key')

      expect(storage.getItem('new.key')).toBe('value123')
    })

    it('should remove old key after migration', () => {
      storage.setItem('old.key', 'value123')
      migrateStorageKey(storage, 'old.key', 'new.key')

      expect(storage.getItem('old.key')).toBeNull()
    })

    it('should not overwrite existing new key', () => {
      storage.setItem('old.key', 'oldValue')
      storage.setItem('new.key', 'existingValue')
      migrateStorageKey(storage, 'old.key', 'new.key')

      expect(storage.getItem('new.key')).toBe('existingValue')
    })

    it('should still remove old key even if new key exists', () => {
      storage.setItem('old.key', 'oldValue')
      storage.setItem('new.key', 'existingValue')
      migrateStorageKey(storage, 'old.key', 'new.key')

      expect(storage.getItem('old.key')).toBeNull()
    })

    it('should be a no-op when old key is absent', () => {
      storage.setItem('new.key', 'existingValue')
      migrateStorageKey(storage, 'old.key', 'new.key')

      expect(storage.getItem('new.key')).toBe('existingValue')
      expect(storage.getItem('old.key')).toBeNull()
    })

    it('should swallow exceptions from a throwing Storage stub', () => {
      const throwingStorage: Storage = {
        length: 0,
        clear: () => {
          throw new Error('SecurityError')
        },
        getItem: () => {
          throw new Error('SecurityError')
        },
        setItem: () => {
          throw new Error('SecurityError')
        },
        removeItem: () => {
          throw new Error('SecurityError')
        },
        key: () => null,
      }

      // Should not throw
      expect(() => {
        migrateStorageKey(throwingStorage, 'old.key', 'new.key')
      }).not.toThrow()
    })
  })

  describe('migrateStorageNamespace (#1368)', () => {
    let storage: Storage

    beforeEach(() => {
      storage = window.localStorage
      storage.clear()
    })

    it('moves a pre-rebrand key forward and returns the current key', () => {
      storage.setItem('ccswitch.theme', 'dark')
      expect(migrateStorageNamespace(storage, storageKey('theme'))).toBe('viberpit.theme')
      expect(storage.getItem('viberpit.theme')).toBe('dark')
      expect(storage.getItem('ccswitch.theme')).toBeNull()
    })

    it('prefers a value already stored under the new key, and clears the old one', () => {
      storage.setItem('ccswitch.theme', 'dark')
      storage.setItem('viberpit.theme', 'light')
      migrateStorageNamespace(storage, storageKey('theme'))
      expect(storage.getItem('viberpit.theme')).toBe('light')
      expect(storage.getItem('ccswitch.theme')).toBeNull()
    })

    it('is a no-op in the steady state — no legacy key, nothing written', () => {
      storage.setItem('viberpit.theme', 'light')
      migrateStorageNamespace(storage, storageKey('theme'))
      expect(Object.keys(storage)).toEqual(['viberpit.theme'])
    })

    it('handles a dynamically named key (draft.<sessionId>)', () => {
      storage.setItem('ccswitch.draft.s1', '{"text":"hi"}')
      migrateStorageNamespace(storage, storageKey('draft.s1'))
      expect(storage.getItem('viberpit.draft.s1')).toBe('{"text":"hi"}')
    })

    it('leaves a key outside the namespace alone rather than inventing a twin', () => {
      storage.setItem('prdash.notifications', 'true')
      expect(migrateStorageNamespace(storage, 'prdash.notifications')).toBe('prdash.notifications')
      expect(storage.getItem('prdash.notifications')).toBe('true')
      expect(Object.keys(storage)).toEqual(['prdash.notifications'])
    })
  })
})
