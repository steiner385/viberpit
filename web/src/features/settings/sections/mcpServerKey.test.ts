import { describe, expect, it } from 'vitest'
import { serverKey } from './mcpServerKey'

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
