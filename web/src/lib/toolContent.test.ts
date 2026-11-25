// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { stripAnsi, coerceText, buildLines, sizeHint, isRasterImagePath, RASTER_DATA_URL_RE, RASTER_MEDIA_TYPE_RE } from './toolContent'

describe('stripAnsi', () => {
  it('removes SGR color codes', () => {
    expect(stripAnsi('\x1b[31mred\x1b[0m')).toBe('red')
  })

  it('removes non-color CSI sequences (erase line, cursor hide)', () => {
    expect(stripAnsi('a\x1b[2Kb\x1b[?25lc')).toBe('abc')
  })

  it('removes OSC sequences terminated by BEL', () => {
    expect(stripAnsi('\x1b]0;title\x07done')).toBe('done')
  })

  it('collapses carriage-return progress overwrites to the final segment', () => {
    expect(stripAnsi('10%\r50%\r100%')).toBe('100%')
  })
})

describe('coerceText', () => {
  it('returns a string unchanged', () => {
    expect(coerceText('print(1)\n')).toBe('print(1)\n')
  })

  it('joins text blocks from an array of content blocks', () => {
    const blocks = [
      { type: 'text', text: 'line one' },
      { type: 'text', text: 'line two' },
    ]
    expect(coerceText(blocks)).toBe('line one\nline two')
  })

  it('labels non-text blocks instead of dumping JSON', () => {
    const blocks = [
      { type: 'text', text: 'see ref' },
      { type: 'tool_reference', tool_name: 'Monitor' },
    ]
    expect(coerceText(blocks)).toBe('see ref\n[tool_reference]')
  })

  it('returns empty string for null/undefined', () => {
    expect(coerceText(null)).toBe('')
    expect(coerceText(undefined)).toBe('')
  })
})

describe('buildLines', () => {
  it('renders an Edit as removed old lines then added new lines, from the tool INPUT', () => {
    // old_string/new_string live in the tool input, NOT in the result payload.
    expect(buildLines({ old_string: 'a\nb', new_string: 'a\nc\nd' })).toEqual([
      { prefix: '-', text: 'a' },
      { prefix: '-', text: 'b' },
      { prefix: '+', text: 'a' },
      { prefix: '+', text: 'c' },
      { prefix: '+', text: 'd' },
    ])
  })

  it('renders a Write (content only) as all-added lines', () => {
    expect(buildLines({ content: 'x\ny' })).toEqual([
      { prefix: '+', text: 'x' },
      { prefix: '+', text: 'y' },
    ])
  })

  it('does NOT treat the result success message as a diff (no edit fields → no lines)', () => {
    expect(buildLines({})).toEqual([])
  })
})

describe('sizeHint', () => {
  it('counts edit churn from the tool INPUT, not the result success message', () => {
    const input = { old_string: 'a\nb', new_string: 'a\nc\nd' }
    const result = { content: 'The file has been updated.' } // must be ignored
    expect(sizeHint('edit', input, result)).toBe('+3 −2')
  })

  it('reports added lines for a Write from input content', () => {
    expect(sizeHint('write', { content: 'x\ny\nz' }, {})).toBe('+3')
  })

  it('counts result lines for read/bash from the result payload', () => {
    expect(sizeHint('read', {}, { content: 'one\ntwo' })).toBe('2 lines')
  })

  it('counts list-shaped result content for read/bash', () => {
    const payload = { content: [{ type: 'text', text: 'one\ntwo\nthree' }] }
    expect(sizeHint('bash', {}, payload)).toBe('3 lines')
  })
})

describe('raster image types (one list, three derived checks)', () => {
  it('matches the same four types as a media type, a data: URL and a path', () => {
    for (const t of ['png', 'jpeg', 'gif', 'webp']) {
      expect(RASTER_MEDIA_TYPE_RE.test(`image/${t}`)).toBe(true)
      expect(RASTER_DATA_URL_RE.test(`data:image/${t};base64,iVBORw0KGgo=`)).toBe(true)
      expect(isRasterImagePath(`/repo/shot.${t.toUpperCase()}`)).toBe(true)
    }
    expect(isRasterImagePath('a/b.jpg')).toBe(true)
    expect(RASTER_MEDIA_TYPE_RE.test('image/svg+xml')).toBe(false)
    expect(RASTER_DATA_URL_RE.test('data:image/svg+xml;base64,PHN2Zz4=')).toBe(false)
    expect(RASTER_DATA_URL_RE.test('data:image/png;base64,a_b')).toBe(false)
    expect(isRasterImagePath('notes.png.md')).toBe(false)
  })
})
