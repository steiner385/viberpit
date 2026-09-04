import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

vi.mock('../pcmCapture', () => ({
  PCM_SAMPLE_RATE: 16000,
  pcmCaptureSupported: vi.fn(() => true),
  startPcmCapture: vi.fn(),
}))
import { pcmCaptureSupported, startPcmCapture } from '../pcmCapture'
import { useServerDictation, STT_SOCKET_PATH } from '../useServerDictation'

class MockSocket {
  static instances: MockSocket[] = []
  static OPEN = 1; static CLOSED = 3
  readyState = 0
  binaryType = ''
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((e: { code: number; reason: string }) => void) | null = null
  url: string
  constructor(url: string) { this.url = url; MockSocket.instances.push(this) }
  send(d: unknown) { this.sent.push(d) }
  close(code = 1000, reason = '') { this.readyState = 3; this.onclose?.({ code, reason }) }
  open() { this.readyState = 1; this.onopen?.() }
  message(m: object) { this.onmessage?.({ data: JSON.stringify(m) }) }
}

let capture: { stop: ReturnType<typeof vi.fn>; onChunk: (c: Int16Array) => void }
beforeEach(() => {
  MockSocket.instances = []
  vi.stubGlobal('WebSocket', MockSocket)
  ;(startPcmCapture as unknown as ReturnType<typeof vi.fn>).mockImplementation(async (onChunk: (c: Int16Array) => void) => {
    capture = { stop: vi.fn(async () => {}), onChunk }
    return capture
  })
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('useServerDictation', () => {
  it('reports unsupported when capture is unsupported', () => {
    ;(pcmCaptureSupported as unknown as ReturnType<typeof vi.fn>).mockReturnValueOnce(false)
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(false)
  })

  it('opens the socket, sends start, streams chunks as binary', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    expect(result.current.listening).toBe(true)
    const ws = MockSocket.instances[0]
    expect(ws.url).toBe(STT_SOCKET_PATH)
    expect(ws.binaryType).toBe('arraybuffer')
    act(() => { ws.open() })
    expect(ws.sent[0]).toBe(JSON.stringify({ type: 'start', sampleRate: 16000 }))
    await flush()
    expect(startPcmCapture).toHaveBeenCalledTimes(1)
    const chunk = new Int16Array([1, 2])
    act(() => { capture.onChunk(chunk) })
    expect(ws.sent[1]).toBe(chunk.buffer)
  })

  it('maps interim and final messages to onResult', () => {
    const onResult = vi.fn()
    const { result } = renderHook(() => useServerDictation({ onResult }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open(); ws.message({ type: 'ready' }); ws.message({ type: 'interim', text: 'hel' }) })
    expect(onResult).toHaveBeenCalledWith('hel', false)
    act(() => { ws.message({ type: 'final', text: 'hello' }) })
    expect(onResult).toHaveBeenCalledWith('hello', true)
  })

  it('stop releases the mic, sends stop, and closes after done', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open() })
    await flush()
    act(() => { result.current.stop() })
    expect(capture.stop).toHaveBeenCalled()
    expect(ws.sent.at(-1)).toBe(JSON.stringify({ type: 'stop' }))
    expect(result.current.listening).toBe(false)
    act(() => { ws.message({ type: 'done' }) })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    // a fresh start after done opens a new socket
    act(() => { result.current.start() })
    expect(MockSocket.instances).toHaveLength(2)
  })

  it('toggle is start when idle, stop when listening, ignored while finishing', async () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.toggle() })
    expect(MockSocket.instances).toHaveLength(1)
    act(() => { MockSocket.instances[0].open() })
    await flush()
    act(() => { result.current.toggle() })       // stop
    act(() => { result.current.toggle() })       // finishing: ignored
    expect(MockSocket.instances).toHaveLength(1)
    expect(MockSocket.instances[0].sent.filter((s) => s === JSON.stringify({ type: 'stop' }))).toHaveLength(1)
  })

  it('surfaces server errors and 4xxx close reasons', () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open(); ws.message({ type: 'error', message: 'model download failed' }) })
    expect(result.current.error).toBe('model download failed')
    act(() => { ws.close(4004, 'stt disabled') })
    expect(result.current.listening).toBe(false)
    expect(result.current.error).toBe('stt disabled')
  })

  it('surfaces a denied microphone and tears down', async () => {
    ;(startPcmCapture as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { MockSocket.instances[0].open() })
    await flush()
    expect(result.current.error).toBe('NotAllowedError')
    expect(result.current.listening).toBe(false)
    expect(MockSocket.instances[0].readyState).toBe(MockSocket.CLOSED)
  })

  it('tears down on unmount', async () => {
    const { result, unmount } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { MockSocket.instances[0].open() })
    await flush()
    unmount()
    expect(capture.stop).toHaveBeenCalled()
    expect(MockSocket.instances[0].readyState).toBe(MockSocket.CLOSED)
  })

  it('a capture that resolves after stop() is released immediately, not kept', async () => {
    let resolveCapture!: (cap: { stop: ReturnType<typeof vi.fn> }) => void
    ;(startPcmCapture as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((resolve) => { resolveCapture = resolve })
    )
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open() })
    act(() => { result.current.stop() })
    const lateCapture = { stop: vi.fn(async () => {}) }
    await act(async () => {
      resolveCapture(lateCapture)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(lateCapture.stop).toHaveBeenCalled()
  })

  it('stop() while the socket is still CONNECTING closes it without sending a stop frame', () => {
    const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    expect(ws.readyState).toBe(0) // CONNECTING, never opened
    act(() => { result.current.stop() })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(result.current.listening).toBe(false)
    expect(ws.sent.some((s) => s === JSON.stringify({ type: 'stop' }))).toBe(false)
  })

  it('closes the socket after the stop grace period when done never arrives', async () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useServerDictation({ onResult: vi.fn() }))
      act(() => { result.current.start() })
      const ws = MockSocket.instances[0]
      act(() => { ws.open() })
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      act(() => { result.current.stop() })
      expect(ws.readyState).not.toBe(MockSocket.CLOSED)
      act(() => { vi.advanceTimersByTime(3000) })
      expect(ws.readyState).toBe(MockSocket.CLOSED)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not call onResult after unmount even if the old socket would dispatch a buffered message', async () => {
    const onResult = vi.fn()
    const { result, unmount } = renderHook(() => useServerDictation({ onResult }))
    act(() => { result.current.start() })
    const ws = MockSocket.instances[0]
    act(() => { ws.open() })
    await flush()
    unmount()
    expect(ws.onmessage).toBeNull()
    expect(ws.onclose).toBeNull()
    // Simulate a frame that was already in flight when unmount ran, if a handler
    // were still attached (it must not be).
    ws.onmessage?.({ data: JSON.stringify({ type: 'final', text: 'late' }) })
    ws.onclose?.({ code: 1000, reason: '' })
    expect(onResult).not.toHaveBeenCalled()
  })
})
