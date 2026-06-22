import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useDictation } from '../useDictation'

// A controllable mock of the browser SpeechRecognition object.
class MockRecognition {
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => { this.onend?.() })

  // test helper: emit one result
  emit(transcript: string, isFinal: boolean) {
    this.onresult?.({
      resultIndex: 0,
      results: [Object.assign([{ transcript }], { isFinal })],
    })
  }
}

let instance: MockRecognition | null = null
function installMock() {
  instance = null
  vi.stubGlobal('SpeechRecognition', class {
    constructor() {
      instance = new MockRecognition()
      return instance
    }
  } as unknown as new () => unknown)
}

afterEach(() => { vi.unstubAllGlobals(); instance = null })

describe('useDictation', () => {
  it('reports unsupported when no SpeechRecognition global exists', () => {
    vi.stubGlobal('SpeechRecognition', undefined)
    vi.stubGlobal('webkitSpeechRecognition', undefined)
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(false)
  })

  it('starts listening and configures continuous + interim recognition', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(true)
    act(() => { result.current.start() })
    expect(result.current.listening).toBe(true)
    expect(instance!.continuous).toBe(true)
    expect(instance!.interimResults).toBe(true)
    expect(instance!.lang).toBe('en-US')
    expect(instance!.start).toHaveBeenCalled()
  })

  it('forwards final and interim transcripts to onResult', () => {
    installMock()
    const onResult = vi.fn()
    const { result } = renderHook(() => useDictation({ onResult }))
    act(() => { result.current.start() })
    act(() => { instance!.emit('hello', false) })
    expect(onResult).toHaveBeenCalledWith('hello', false)
    act(() => { instance!.emit('hello world', true) })
    expect(onResult).toHaveBeenCalledWith('hello world', true)
  })

  it('stops listening on stop()', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { result.current.stop() })
    expect(result.current.listening).toBe(false)
  })

  it('records an error and stops listening on recognition error', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { instance!.onerror?.({ error: 'not-allowed' }) })
    expect(result.current.error).toBe('not-allowed')
    expect(result.current.listening).toBe(false)
  })
})
