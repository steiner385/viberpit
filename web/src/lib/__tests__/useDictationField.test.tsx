import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { useRef, useState } from 'react'
import { useDictationField } from '../useDictationField'

class MockRecognition {
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => { this.onend?.() })
  emit(transcript: string, isFinal: boolean) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript }], { isFinal })] })
  }
}

let instance: MockRecognition
afterEach(() => { vi.unstubAllGlobals() })

// A harness exposing the field hook over a real textarea + controlled state.
let api: ReturnType<typeof useDictationField>
let currentValue = ''
function Harness({ initial = '', caret }: { initial?: string; caret?: number }) {
  const [value, setValue] = useState(initial)
  // eslint-disable-next-line react-hooks/globals
  currentValue = value
  const ref = useRef<HTMLTextAreaElement>(null)
  // eslint-disable-next-line react-hooks/globals
  api = useDictationField({ value, setValue, textareaRef: ref })
  return (
    <textarea
      ref={ref}
      data-testid="ta"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      // place the caret where the test wants it
      onFocus={(e) => { if (caret != null) e.currentTarget.setSelectionRange(caret, caret) }}
    />
  )
}

function installMock() {
  vi.stubGlobal('SpeechRecognition', class {
    constructor() {
      instance = new MockRecognition()
      return instance
    }
  } as unknown as typeof MockRecognition)
}

describe('useDictationField', () => {
  it('appends a final transcript at the end with a trailing space', () => {
    installMock()
    render(<Harness initial="" />)
    act(() => { api.start() })
    act(() => { instance.emit('hello world', true) })
    expect(currentValue).toBe('hello world ')
  })

  it('replaces interim text live, then commits the final text', () => {
    installMock()
    render(<Harness initial="" />)
    act(() => { api.start() })
    act(() => { instance.emit('hel', false) })
    expect(currentValue).toBe('hel')
    act(() => { instance.emit('hello', false) })
    expect(currentValue).toBe('hello')
    act(() => { instance.emit('hello there', true) })
    expect(currentValue).toBe('hello there ')
  })

  it('inserts at the caret position within existing text', () => {
    installMock()
    const { getByTestId } = render(<Harness initial="ab" caret={1} />)
    act(() => { (getByTestId('ta') as HTMLTextAreaElement).focus() })
    act(() => { api.start() })
    act(() => { instance.emit('X', true) })
    // caret was between 'a' and 'b' → "a" + "X " + "b"
    expect(currentValue).toBe('aX b')
  })
})
