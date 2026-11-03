import { describe, it, expect } from 'vitest'
import { highlightRanges, highlightSegments, maskMarkdownCode, parseSearchTerms } from './highlightRanges'

describe('highlightRanges', () => {
  it('finds every occurrence of every term, sorted', () => {
    expect(highlightRanges('foo bar foo', ['foo', 'bar'])).toEqual([[0, 3], [4, 7], [8, 11]])
  })

  it('is case-insensitive in both the text and the terms', () => {
    expect(highlightRanges('Hello HELLO hello', ['hello'])).toEqual([[0, 5], [6, 11], [12, 17]])
    expect(highlightRanges('Hello', ['HeLLo'])).toEqual([[0, 5]])
  })

  it('merges overlapping and touching ranges into one', () => {
    // "abc" [0,3) and "bcd" [1,4) overlap → [0,4); "de" [3,5) then extends it.
    expect(highlightRanges('abcde', ['abc', 'bcd', 'de'])).toEqual([[0, 5]])
    // Touching: "ab" [0,2) and "cd" [2,4) are one highlight.
    expect(highlightRanges('abcd', ['ab', 'cd'])).toEqual([[0, 4]])
  })

  it('treats a term as a literal — never a regex', () => {
    expect(highlightRanges('a.*b axxb', ['a.*b'])).toEqual([[0, 4]])
    expect(highlightRanges('(x) [y] $1 ^z', ['(x)', '[y]', '$1', '^z'])).toEqual([[0, 3], [4, 7], [8, 10], [11, 13]])
    expect(highlightRanges('nothing here', ['.*'])).toEqual([])
  })

  it('ignores empty terms and an empty text', () => {
    expect(highlightRanges('abc', ['', 'b'])).toEqual([[1, 2]])
    expect(highlightRanges('', ['a'])).toEqual([])
    expect(highlightRanges('abc', [])).toEqual([])
  })

  it('counts overlapping repeats of one term', () => {
    expect(highlightRanges('aaaa', ['aa'])).toEqual([[0, 4]])
  })

  it('keeps offsets into the ORIGINAL text when lowercasing changes its length', () => {
    // "İ".toLowerCase() is two code units; the "x" after it is still at index 1.
    const text = '\u0130x'
    expect(highlightRanges(text, ['x'])).toEqual([[1, 2]])
    const [[s, e]] = highlightRanges(text, ['\u0069\u0307'])
    expect(text.slice(s, e)).toBe('\u0130')
  })
})

describe('highlightSegments', () => {
  it('splits text into plain and marked runs, flagging the runs that hold the lead term', () => {
    expect(highlightSegments('a foo b bar', ['foo', 'bar'])).toEqual([
      { text: 'a ', mark: false, lead: false },
      { text: 'foo', mark: true, lead: true },
      { text: ' b ', mark: false, lead: false },
      { text: 'bar', mark: true, lead: false },
    ])
  })

  it('returns the whole text as one plain run when nothing matches', () => {
    expect(highlightSegments('plain', ['zzz'])).toEqual([{ text: 'plain', mark: false, lead: false }])
  })
})

describe('parseSearchTerms', () => {
  it('lowercases and splits on whitespace, like the server\'s parse_terms', () => {
    expect(parseSearchTerms('  Foo\tBAR  baz ')).toEqual(['foo', 'bar', 'baz'])
    expect(parseSearchTerms('   ')).toEqual([])
  })
})

describe('maskMarkdownCode', () => {
  it('blanks inline code and fenced blocks, keeping every offset and newline', () => {
    const text = 'Run `a widget` now\n```ts\nwidget()\n```\nthe widget'
    const masked = maskMarkdownCode(text)
    expect(masked).toHaveLength(text.length)
    expect(masked.split('\n')).toHaveLength(text.split('\n').length)
    expect(masked).not.toContain('`')
    expect(highlightRanges(masked, ['widget'])).toEqual([[text.lastIndexOf('widget'), text.length]])
  })

  it('an unclosed fence blanks to the end; a fence closes only on its own kind and length', () => {
    expect(maskMarkdownCode('a\n````\nb\n```\nc').trim()).toBe('a')
    expect(maskMarkdownCode('~~~\nx\n~~~\ny')).toBe('   \n \n   \ny')
  })

  it('leaves text without code untouched', () => {
    expect(maskMarkdownCode('plain widget text')).toBe('plain widget text')
  })
})
