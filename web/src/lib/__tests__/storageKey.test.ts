import { describe, it, expect, beforeEach } from 'vitest'
import { STORAGE_NAMESPACE, storageKey, migrateStorageKey } from '../storageKey'

describe('storageKey', () => {
  describe('STORAGE_NAMESPACE', () => {
    it('should be "ccswitch"', () => {
      expect(STORAGE_NAMESPACE).toBe('ccswitch')
    })
  })

  describe('storageKey', () => {
    it('should prefix name with namespace', () => {
      expect(storageKey('theme')).toBe('ccswitch.theme')
    })

    it('should handle various names', () => {
      expect(storageKey('listWidth')).toBe('ccswitch.listWidth')
      expect(storageKey('pipelineNotifications')).toBe('ccswitch.pipelineNotifications')
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
})
