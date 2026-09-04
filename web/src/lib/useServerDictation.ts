import { useCallback, useEffect, useRef, useState } from 'react'
import type { UseDictation, UseDictationOptions } from './useDictation'
import { PCM_SAMPLE_RATE, pcmCaptureSupported, startPcmCapture, type PcmCapture } from './pcmCapture'

/** Same-origin path; the existing session socket uses a relative `/ws` the same way. */
export const STT_SOCKET_PATH = '/ws/stt'
const STOP_GRACE_MS = 3000

type ServerMsg =
  | { type: 'ready' }
  | { type: 'interim' | 'final'; text: string }
  | { type: 'error'; message: string }
  | { type: 'done' }

/**
 * Dictation via the edge's streaming Whisper engine (`/ws/stt`): mic PCM goes up as
 * binary frames, `interim`/`final` transcript events come back. Same shape as the
 * Web Speech `useDictation` so `useDictationField` can drive either.
 */
export function useServerDictation({ onResult }: UseDictationOptions): UseDictation {
  const [supported] = useState(() => pcmCaptureSupported())
  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const captureRef = useRef<PcmCapture | null>(null)
  const finishingRef = useRef(false)
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  const releaseMic = useCallback(() => {
    const cap = captureRef.current
    captureRef.current = null
    void cap?.stop()
  }, [])

  const teardown = useCallback((ws: WebSocket) => {
    if (wsRef.current !== ws) return
    wsRef.current = null
    finishingRef.current = false
    releaseMic()
    setListening(false)
  }, [releaseMic])

  const stop = useCallback(() => {
    const ws = wsRef.current
    if (!ws || finishingRef.current) return
    finishingRef.current = true
    releaseMic()
    setListening(false)
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stop' }))
      // The server closes after `done`; if it never arrives, do not hang forever.
      setTimeout(() => { if (ws.readyState !== WebSocket.CLOSED) ws.close() }, STOP_GRACE_MS)
    } else {
      ws.close()
    }
  }, [releaseMic])

  const start = useCallback(() => {
    if (wsRef.current) return
    setError(null)
    const ws = new WebSocket(STT_SOCKET_PATH)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    finishingRef.current = false
    setListening(true)

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'start', sampleRate: PCM_SAMPLE_RATE }))
      startPcmCapture((chunk) => { if (ws.readyState === WebSocket.OPEN) ws.send(chunk) })
        .then((cap) => {
          if (wsRef.current !== ws || finishingRef.current) { void cap.stop(); return }
          captureRef.current = cap
        })
        .catch((e: unknown) => {
          setError(e instanceof Error && e.name ? e.name : 'capture-error')
          ws.close()
        })
    }
    ws.onmessage = (ev: MessageEvent) => {
      let msg: ServerMsg
      try { msg = JSON.parse(String(ev.data)) as ServerMsg } catch { return }
      switch (msg.type) {
        case 'interim': onResultRef.current(msg.text, false); break
        case 'final': onResultRef.current(msg.text, true); break
        case 'error': setError(msg.message || 'stt-error'); break
        case 'done': ws.close(); break
        case 'ready': break
      }
    }
    ws.onerror = () => { setError('socket-error') }
    ws.onclose = (ev: CloseEvent) => {
      if (ev.code >= 4000) setError(ev.reason || `stt-${ev.code}`)
      teardown(ws)
    }
  }, [teardown])

  const toggle = useCallback(() => {
    if (wsRef.current) stop()
    else start()
  }, [start, stop])

  useEffect(() => () => {
    const ws = wsRef.current
    wsRef.current = null
    releaseMic()
    ws?.close()
  }, [releaseMic])

  return { supported, listening, error, start, stop, toggle }
}
