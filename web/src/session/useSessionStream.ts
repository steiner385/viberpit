import { useEffect, useReducer } from 'react'
import { useSessionSocket } from '@/ws/client'
import type { WsRotatedMsg, WsErrorMsg } from '@/ws/client'
import { transcriptReducer, initialTranscript } from './reducer'
import type { SessionEvent } from './events'

export function useSessionStream(sessionId: string, onRotated?: (m: WsRotatedMsg) => void) {
  const sock = useSessionSocket()
  const [state, dispatch] = useReducer(transcriptReducer, initialTranscript)

  useEffect(() => {
    const onEvent = (e: SessionEvent) => dispatch(e)
    const onErr = (m: WsErrorMsg) =>
      dispatch({ type: 'error', code: m.code, message: m.message, resetsAt: m.resetsAt ?? null })
    const onRot = (m: WsRotatedMsg) => { if (m.sessionId === sessionId) onRotated?.(m) }
    sock.on('sessionEvent', onEvent)
    sock.on('rotated', onRot)
    sock.on('error', onErr)
    sock.attach(sessionId)
    return () => {
      sock.off('sessionEvent', onEvent); sock.off('rotated', onRot); sock.off('error', onErr)
      sock.detach(sessionId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  return { state, dispatch, ...sock }
}
