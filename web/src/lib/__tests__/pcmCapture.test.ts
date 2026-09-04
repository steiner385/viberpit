import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  CHUNK_SAMPLES, PCM_SAMPLE_RATE, WORKLET_SOURCE, pcmCaptureSupported, startPcmCapture,
} from '../pcmCapture'

// Evaluate the worklet source in a fake AudioWorklet realm so the conversion code
// that actually ships is what gets tested.
function loadProcessor() {
  let registered: (new () => { process(inputs: Float32Array[][]): boolean; port: { postMessage: ReturnType<typeof vi.fn> } }) | null = null
  const realm = {
    AudioWorkletProcessor: class { port = { postMessage: vi.fn() } },
    registerProcessor: (_name: string, cls: never) => { registered = cls },
  }
  new Function('AudioWorkletProcessor', 'registerProcessor', WORKLET_SOURCE)(realm.AudioWorkletProcessor, realm.registerProcessor)
  return registered!
}

afterEach(() => { vi.unstubAllGlobals() })

describe('worklet processor', () => {
  it('converts float samples to clamped Int16 and posts CHUNK_SAMPLES at a time', () => {
    const Proc = loadProcessor()
    const p = new Proc()
    const frame = new Float32Array(128)
    frame[0] = 1; frame[1] = -1; frame[2] = 0.5; frame[3] = 2 /* clipped */
    for (let i = 0; i < CHUNK_SAMPLES / 128; i++) expect(p.process([[frame]])).toBe(true)
    expect(p.port.postMessage).toHaveBeenCalledTimes(1)
    const chunk = p.port.postMessage.mock.calls[0][0] as Int16Array
    expect(chunk).toBeInstanceOf(Int16Array)
    expect(chunk.length).toBe(CHUNK_SAMPLES)
    expect([chunk[0], chunk[1], chunk[2], chunk[3]]).toEqual([32767, -32768, 16383, 32767])
  })

  it('keeps running with no input connected', () => {
    const Proc = loadProcessor()
    expect(new Proc().process([[]])).toBe(true)
  })
})

describe('startPcmCapture', () => {
  function stubAudio() {
    const tracks = [{ stop: vi.fn() }]
    const stream = { getTracks: () => tracks }
    const getUserMedia = vi.fn(async () => stream)
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
    const port = { onmessage: null as null | ((e: { data: unknown }) => void) }
    const node = { port, connect: vi.fn(), disconnect: vi.fn() }
    const source = { connect: vi.fn(), disconnect: vi.fn() }
    const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }
    const ctx = {
      sampleRate: PCM_SAMPLE_RATE, destination: {},
      audioWorklet: { addModule: vi.fn(async () => {}) },
      createMediaStreamSource: vi.fn(() => source),
      createGain: vi.fn(() => gain),
      resume: vi.fn(async () => {}), close: vi.fn(async () => {}),
    }
    const AudioContext = vi.fn(function() { return ctx })
    vi.stubGlobal('AudioContext', AudioContext)
    vi.stubGlobal('AudioWorkletNode', vi.fn(function() { return node }))
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() })
    return { tracks, getUserMedia, ctx, AudioContext, node, source, gain, port }
  }

  it('reports unsupported without getUserMedia or AudioWorkletNode', () => {
    vi.stubGlobal('navigator', {})
    expect(pcmCaptureSupported()).toBe(false)
  })

  it('opens a 16 kHz mono context, loads the worklet, and forwards chunks', async () => {
    const s = stubAudio()
    const onChunk = vi.fn()
    const cap = await startPcmCapture(onChunk)
    expect(s.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    })
    expect(s.AudioContext).toHaveBeenCalledWith({ sampleRate: PCM_SAMPLE_RATE })
    expect(s.ctx.audioWorklet.addModule).toHaveBeenCalledWith('blob:x')
    expect(s.source.connect).toHaveBeenCalledWith(s.node)
    expect(s.gain.gain.value).toBe(0)
    const chunk = new Int16Array([1, 2, 3])
    s.port.onmessage!({ data: chunk })
    expect(onChunk).toHaveBeenCalledWith(chunk)
    await cap.stop()
    expect(s.tracks[0].stop).toHaveBeenCalled()
    expect(s.ctx.close).toHaveBeenCalled()
    expect(s.source.disconnect).toHaveBeenCalled()
  })

  it('releases the mic if the worklet fails to load', async () => {
    const s = stubAudio()
    s.ctx.audioWorklet.addModule.mockRejectedValueOnce(new Error('nope'))
    await expect(startPcmCapture(vi.fn())).rejects.toThrow('nope')
    expect(s.tracks[0].stop).toHaveBeenCalled()
    expect(s.ctx.close).toHaveBeenCalled()
  })
})
