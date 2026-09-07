import { describe, it, expect } from 'vitest'
import { hideHtmlComments } from '../htmlComments'

describe('hideHtmlComments', () => {
  it('removes a completed HTML comment', () => {
    expect(hideHtmlComments('done!\n<!-- viberpit:complete -->')).toBe('done!')
  })

  it('removes the pre-rebrand spelling too — it matches no marker vocabulary at all', () => {
    expect(hideHtmlComments('done!\n<!-- ccswitch:no-pr -->')).toBe('done!')
  })

  it('removes a marker carrying attributes', () => {
    const text = 'switching\n<!-- viberpit:model=opus reason="harder than it looked" -->'
    expect(hideHtmlComments(text)).toBe('switching')
  })

  it('removes several comments in one message', () => {
    expect(hideHtmlComments('a <!-- one --> b <!-- two --> c')).toBe('a  b  c')
  })

  it('removes a comment still arriving, so it never flickers into view', () => {
    // Mid-stream: the opening delimiter has landed but `-->` has not.
    expect(hideHtmlComments('done!\n<!-- viberp')).toBe('done!')
  })

  it('leaves a lone `<` or an unopened comment alone', () => {
    expect(hideHtmlComments('a < b and 3 --> 4')).toBe('a < b and 3 --> 4')
  })

  it('leaves text with no comment untouched, including trailing whitespace', () => {
    expect(hideHtmlComments('still typing ')).toBe('still typing ')
  })

  it('keeps a comment inside a fenced code block', () => {
    // It renders as literal text once the block settles, so stripping it here
    // would make real content flicker out and back in.
    const text = 'edit this:\n```html\n<!-- a real comment -->\n```'
    expect(hideHtmlComments(text)).toBe(text)
  })

  it('still strips a marker that follows a fenced code block', () => {
    const text = '```html\n<!-- kept -->\n```\nall set\n<!-- viberpit:complete -->'
    expect(hideHtmlComments(text)).toBe('```html\n<!-- kept -->\n```\nall set')
  })

  it('keeps a partial comment inside an unclosed fence that is still streaming', () => {
    const text = 'writing:\n```html\n<!-- half wri'
    expect(hideHtmlComments(text)).toBe(text)
  })

  it('is a no-op on empty text', () => {
    expect(hideHtmlComments('')).toBe('')
  })
})
