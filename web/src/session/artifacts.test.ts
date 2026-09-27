// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { LEGACY_MCP_TOOL_PREFIX } from '@/brand'
import { artifactFromBlock, isArtifactBlock } from './artifacts'
import type { Block } from './reducer'

type ToolBlock = Extract<Block, { kind: 'tool' }>

function envelope(data: Record<string, unknown>): Record<string, unknown> {
  return { content: [{ type: 'text', text: JSON.stringify({ source: 'viberpit', now: 't', data }) }] }
}

function share(input: Record<string, unknown>, extra: Partial<ToolBlock> = {}): ToolBlock {
  return {
    kind: 'tool', id: 'b1', toolId: 'b1', name: 'mcp__viberpit__share_artifact',
    toolKind: 'app_action', input, ...extra,
  }
}

describe('isArtifactBlock', () => {
  it('matches share_artifact under the current and legacy MCP prefixes only', () => {
    expect(isArtifactBlock(share({ kind: 'url' }))).toBe(true)
    expect(isArtifactBlock(share({ kind: 'url' }, { name: `${LEGACY_MCP_TOOL_PREFIX}share_artifact` }))).toBe(true)
    expect(isArtifactBlock(share({}, { name: 'mcp__viberpit__set_hold' }))).toBe(false)
    expect(isArtifactBlock({ kind: 'tool', id: 'r', toolId: 'r', name: 'Read', toolKind: 'read', input: {} })).toBe(false)
    expect(isArtifactBlock({ kind: 'text', id: 't', text: 'x' })).toBe(false)
  })
})

describe('artifactFromBlock', () => {
  it('reads kind, value, label and note from the call input before any result lands', () => {
    const a = artifactFromBlock(share({ kind: 'url', value: 'https://g.h/o/n/pull/7', label: 'PR #7', note: 'armed' }))
    expect(a).toEqual({
      kind: 'url', value: 'https://g.h/o/n/pull/7', label: 'PR #7', note: 'armed',
      status: 'pending', url: null, error: null,
    })
  })

  it('takes the served image url from the handler result envelope', () => {
    const block = share(
      { kind: 'image', value: '/tmp/shot.png', label: 'Settings page' },
      { result: { ok: true, payload: envelope({ kind: 'image', value: '/tmp/shot.png', label: 'Settings page', url: '/api/attachments/abc' }) } },
    )
    expect(artifactFromBlock(block)).toMatchObject({ kind: 'image', status: 'ok', url: '/api/attachments/abc' })
  })

  it('surfaces a refused call as an error with the handler message', () => {
    const block = share(
      { kind: 'image', value: '/tmp/nope.png', label: 'x' },
      { result: { ok: false, payload: { content: [{ type: 'text', text: 'no such file on this host: /tmp/nope.png' }] } } },
    )
    expect(artifactFromBlock(block)).toMatchObject({ status: 'error', error: 'no such file on this host: /tmp/nope.png', url: null })
  })

  it('never throws on a malformed input or result — falls back to a path-less card', () => {
    const a = artifactFromBlock(share({ kind: 'video', value: 42 }, { result: { ok: true, payload: { content: 'not json' } } }))
    expect(a).toMatchObject({ kind: 'path', value: '42', label: '', status: 'ok', url: null })
  })
})
