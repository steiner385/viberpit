import { describe, it, expect } from 'vitest'
import { isFilePathRef } from '../filePathRef'

describe('isFilePathRef', () => {
  it.each([
    'web/src/foo.tsx:60',
    'web/src/foo.tsx',
    'foo.py',
    './config.json',
    '/etc/hosts',
    'src/lib/bar.ts',
    'App.tsx:12:4',
    '~/notes.md',
  ])('accepts path-like token %j', (s) => {
    expect(isFilePathRef(s)).toBe(true)
  })

  it.each([
    'npm install',        // whitespace
    'useState',           // bare identifier, no slash or extension
    '--force',            // flag
    '1/2',                // ratio: slash but no path-ish segment/extension
    '',                   // empty
    'a.b c.d',            // whitespace
    'https://example.com',// URL, not a file path
  ])('rejects non-path token %j', (s) => {
    expect(isFilePathRef(s)).toBe(false)
  })
})
