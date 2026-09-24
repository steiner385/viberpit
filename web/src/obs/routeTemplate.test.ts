// @vitest-environment node
// web/src/obs/routeTemplate.test.ts
import { describe, it, expect } from 'vitest'
import { routeTemplate } from './routeTemplate'

describe('routeTemplate', () => {
  it('keeps a static path as-is', () => {
    expect(routeTemplate('/sessions', {})).toBe('/sessions')
    expect(routeTemplate('/', {})).toBe('/')
    expect(routeTemplate('/settings/deploy', {})).toBe('/settings/deploy')
  })

  it('replaces a router param with its name', () => {
    expect(routeTemplate('/sessions/8f14e45f-ceea-467a-9b2e-000000000000', {
      id: '8f14e45f-ceea-467a-9b2e-000000000000',
    })).toBe('/sessions/:id')
  })

  it('replaces every param, including nested ones', () => {
    expect(routeTemplate('/sessions/abc-123/agents/agent-9', { id: 'abc-123', agentId: 'agent-9' }))
      .toBe('/sessions/:id/agents/:agentId')
  })

  it('collapses a multi-segment splat param into one placeholder', () => {
    expect(routeTemplate('/library/docs/a/b/c', { '*': 'docs/a/b/c' })).toBe('/library/:splat')
  })

  it('ignores params whose value is absent from the path', () => {
    expect(routeTemplate('/sessions', { id: undefined })).toBe('/sessions')
  })

  // Defence in depth: the param map is the primary redactor, but a page that
  // reads its id from a search param or renders a path the router matched with
  // a splat must still not ship an identifier.
  it('scrubs an unclaimed id-shaped segment even with no params', () => {
    expect(routeTemplate('/sessions/8f14e45f-ceea-467a-9b2e-000000000000', {}))
      .toBe('/sessions/:id')
    expect(routeTemplate('/delivery/ci/12345', {})).toBe('/delivery/ci/:id')
    expect(routeTemplate('/roadmap/initiative/spec-2026-09-24', {}))
      .toBe('/roadmap/initiative/:id')
  })

  it('leaves word-shaped static segments alone', () => {
    expect(routeTemplate('/sessions/background/archived', {})).toBe('/sessions/background/archived')
    expect(routeTemplate('/settings/permission-profiles', {})).toBe('/settings/permission-profiles')
  })

  it('drops the query string and hash entirely', () => {
    expect(routeTemplate('/sessions?kiosk=1&prompt=secret', {})).toBe('/sessions')
    expect(routeTemplate('/sessions#token', {})).toBe('/sessions')
  })

  it('bounds depth so a pathological URL cannot explode route cardinality', () => {
    const deep = '/' + Array.from({ length: 20 }, (_, i) => `seg${String.fromCharCode(97 + i)}`).join('/')
    const out = routeTemplate(deep, {})
    expect(out.split('/').length - 1).toBeLessThanOrEqual(6)
    expect(out.endsWith('/…')).toBe(true)
  })

  it('never returns an empty string', () => {
    expect(routeTemplate('', {})).toBe('/')
  })
})
