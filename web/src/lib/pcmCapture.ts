/**
 * Microphone → 16 kHz mono Int16 PCM chunks, for the server-side dictation engine.
 *
 * The AudioContext is opened at 16 kHz so the browser resamples the mic for us; an
 * AudioWorklet (loaded from a Blob URL — no build config) converts Float32 frames to
 * Int16 and posts fixed-size chunks (~100 ms) to the main thread.
 */
export const PCM_SAMPLE_RATE = 16000
export const CHUNK_SAMPLES = 1600 // 100 ms at 16 kHz

export const WORKLET_SOURCE = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this._buf = new Int16Array(${CHUNK_SAMPLES})
    this._n = 0
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]))
      this._buf[this._n++] = s < 0 ? s * 0x8000 : s * 0x7fff
      if (this._n === this._buf.length) {
        this.port.postMessage(this._buf, [this._buf.buffer])
        this._buf = new Int16Array(${CHUNK_SAMPLES})
        this._n = 0
      }
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCaptureProcessor)
`

export interface PcmCapture {
  stop(): Promise<void>
}

export function pcmCaptureSupported(): boolean {
  return typeof navigator !== 'undefined'
    && typeof navigator.mediaDevices?.getUserMedia === 'function'
    && typeof AudioWorkletNode !== 'undefined'
}

export async function startPcmCapture(onChunk: (chunk: Int16Array) => void): Promise<PcmCapture> {
  if (!pcmCaptureSupported()) throw new Error('audio-capture-unsupported')
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  })
  const ctx = new AudioContext({ sampleRate: PCM_SAMPLE_RATE })
  const release = async () => {
    stream.getTracks().forEach((t) => t.stop())
    await ctx.close()
  }
  // Some browsers ignore the requested rate. Streaming 44.1/48 kHz audio labelled as
  // 16 kHz makes whisper return garbage silently, so fail loudly instead.
  if (ctx.sampleRate !== PCM_SAMPLE_RATE) {
    await release()
    throw new Error('audio-rate-unsupported')
  }
  try {
    const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'application/javascript' }))
    try { await ctx.audioWorklet.addModule(url) } finally { URL.revokeObjectURL(url) }
    await ctx.resume()
  } catch (e) {
    await release()
    throw e
  }
  const source = ctx.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(ctx, 'pcm-capture')
  // A worklet with no downstream connection may be culled; route through a muted gain.
  const sink = ctx.createGain()
  sink.gain.value = 0
  node.port.onmessage = (e: MessageEvent) => onChunk(e.data as Int16Array)
  source.connect(node)
  node.connect(sink)
  sink.connect(ctx.destination)
  return {
    async stop() {
      node.port.onmessage = null
      source.disconnect()
      node.disconnect()
      sink.disconnect()
      await release()
    },
  }
}
