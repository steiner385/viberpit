import { describe, expect, it } from 'vitest'
import { LOGIN_PATH, loginUrlFor, safeNextPath } from './loginRedirect'

describe('safeNextPath', () => {
  it('keeps a same-origin path with its search and hash', () => {
    expect(safeNextPath('/sessions/abc?x=1#y')).toBe('/sessions/abc?x=1#y')
  })

  it.each([
    [null], [''], ['https://evil.example/'], ['//evil.example/x'], ['/\\evil.example'],
    ['javascript:alert(1)'], ['sessions'], [' /sessions'],
  ])('falls back to / for %j', (raw) => {
    expect(safeNextPath(raw)).toBe('/')
  })

  it('never sends a signed-in user back to the login page', () => {
    expect(safeNextPath(LOGIN_PATH)).toBe('/')
    expect(safeNextPath(`${LOGIN_PATH}?next=/x`)).toBe('/')
  })
})

describe('loginUrlFor', () => {
  it('carries the current location as `next`', () => {
    expect(loginUrlFor('/pipeline?kiosk=1')).toBe(`${LOGIN_PATH}?next=%2Fpipeline%3Fkiosk%3D1`)
  })

  it('omits `next` when it would only point home', () => {
    expect(loginUrlFor('/')).toBe(LOGIN_PATH)
  })
})
