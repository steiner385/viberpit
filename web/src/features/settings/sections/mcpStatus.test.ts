import { describe, expect, it } from 'vitest'
import { relativeAge, serverKey, shortName, toneFor, worstStatus } from './mcpStatus'

describe('worstStatus', () => {
  it('ranks failed > needs-auth > pending > connected', () => {
    expect(worstStatus(['connected', 'needs-auth', 'pending'])).toBe('needs-auth')
    expect(worstStatus(['connected', 'failed'])).toBe('failed')
  })
  it('returns null for no rows', () => expect(worstStatus([])).toBeNull())
  it('keeps unknown statuses but ranks them below known problems', () => {
    expect(worstStatus(['connecting'])).toBe('connecting')
    expect(worstStatus(['connecting', 'failed'])).toBe('failed')
  })
})

describe('toneFor', () => {
  it('maps the vocabulary', () => {
    expect(toneFor('failed')).toBe('bad'); expect(toneFor('needs-auth')).toBe('warn')
    expect(toneFor('pending')).toBe('pending'); expect(toneFor('connected')).toBe('ok')
    expect(toneFor('disabled')).toBe('neutral'); expect(toneFor('connecting')).toBe('neutral')
    expect(toneFor(null)).toBe('unknown')
  })
})

it('shortName strips plugin prefix', () => {
  expect(shortName('plugin:railway:railway')).toBe('railway')
  expect(shortName('github')).toBe('github')
})

it('relativeAge reuses the shared relative-time formatter', () => {
  const now = Date.parse('2026-10-09T12:05:00Z')
  expect(relativeAge('2026-10-09T12:02:00Z', now)).toBe('3m ago')
})

// Mirrors server/tests/test_mcp_scope_resolve.py::test_server_key_* (same inputs, same outputs).
describe('serverKey', () => {
  it.each([
    [{ name: 'gh', source: 'user' }, 'gh'],
    [{ name: 'railway', source: 'railway@claude-plugins-official', pluginName: 'railway' }, 'plugin:railway:railway'],
    // Manifest name, not the registry id: the CLI reports plugin:Notion:notion.
    [{ name: 'notion', source: 'notion@claude-plugins-official', pluginName: 'Notion' }, 'plugin:Notion:notion'],
    // No manifest name: fall back to the registry name, as scan.py's _plugin_manifest_name does.
    [{ name: 'notion', source: 'notion@claude-plugins-official' }, 'plugin:notion:notion'],
  ])('%j -> %s', (entry, key) => expect(serverKey(entry)).toBe(key))

  it('keeps a user and a plugin server with the same short name distinct', () => {
    const user = serverKey({ name: 'notion', source: 'user' })
    const plugin = serverKey({ name: 'notion', source: 'notion@claude-plugins-official', pluginName: 'Notion' })
    expect(user).toBe('notion')
    expect(plugin).toBe('plugin:Notion:notion')
    expect(user).not.toBe(plugin)
  })
})
