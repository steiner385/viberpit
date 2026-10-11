import { useEffect, useState } from 'react'
import { getSessionSocket, type ConnectionState } from '@/ws/client'
import { useUpdatePending } from '@/hooks/useUpdatePending'

export interface InputHoldState {
  holding: boolean
  count: number
  /** 'updating' = a deploy drain; 'reconnecting' = the socket is down; 'retrying' = the
   *  socket is up but a session refused a prompt (driver gone/recovering), retried on a
   *  backoff. */
  mode: 'updating' | 'reconnecting' | 'retrying'
  /** Stop waiting and send what is held now. */
  sendNow: () => void
  /** Drop every held message. */
  discard: () => void
}

function tryGetSocket() {
  try {
    return getSessionSocket()
  } catch (e) {
    // jsdom rejects relative WebSocket URLs ('/ws') with SYNTAX_ERR; degrade under test.
    if (e instanceof DOMException && e.code === DOMException.SYNTAX_ERR) return null
    throw e
  }
}

/**
 * How long a socket must stay in `reconnecting` before the banner is raised for it.
 *
 * The banner renders IN FLOW above the content band (see BannerStack / DaemonRestartBanner
 * for why banners are never `fixed`), so showing it moves everything below it down by its
 * own height and hiding it moves everything back. A dropped socket normally comes back on
 * the first backoff attempt (BACKOFF_BASE_MS = 500 ms plus jitter, ~0.6 s observed), and a
 * transcript that jumps ~70px down and back within half a second reads as the content
 * flickering under the pointer. Two seconds covers the first attempt and most of the second
 * (1 s backoff plus the handshake), so only an outage a person would notice raises the banner.
 */
export const RECONNECT_BANNER_GRACE_MS = 2_000

/**
 * True whenever the user's input cannot currently reach a session, so the UI should hold a
 * banner: a deploy drain is in progress, OR prompts are already buffered, OR the socket dropped
 * *after* being connected and has been reconnecting for longer than RECONNECT_BANNER_GRACE_MS.
 *
 * We deliberately key the disconnect case on the `reconnecting` state, NOT `!connected`: a fresh
 * socket starts in `connecting`/`disconnected` during the initial handshake, and treating that as
 * "input held" would flash the banner on every page load (and render a spurious second
 * role="status" in Dashboard tests). `reconnecting` only occurs after a prior connection dropped.
 */
export function useInputHold(): InputHoldState {
  const pending = useUpdatePending()
  const socket = tryGetSocket()
  // True only once the socket has been `reconnecting` for the whole grace. The timer is
  // armed on entering that state and disarmed (lowering the banner at once) on leaving it,
  // so a quick reconnect never fires it and the banner never shows. State moves only from
  // the socket listener and the timer callback — never synchronously inside the effect.
  const [reconnecting, setReconnecting] = useState(false)
  useEffect(() => {
    if (!socket) return
    let timer: ReturnType<typeof setTimeout> | null = null
    const arm = () => {
      timer = setTimeout(() => { timer = null; setReconnecting(true) }, RECONNECT_BANNER_GRACE_MS)
    }
    const ch = (s: ConnectionState) => {
      if (timer !== null) { clearTimeout(timer); timer = null }
      if (s === 'reconnecting') arm()
      else setReconnecting(false)
    }
    socket.on('connectionStateChange', ch)
    // A socket already mid-reconnect at mount gets the same grace as a live drop.
    if (socket.connectionState === 'reconnecting') arm()
    return () => {
      if (timer !== null) clearTimeout(timer)
      socket.off('connectionStateChange', ch)
    }
  }, [socket])
  return {
    holding: pending.active || pending.count > 0 || reconnecting,
    count: pending.count,
    // 'updating' is the richer deploy message. Held messages on a live socket are a
    // session's refusal, not a lost connection — saying "Connection lost" there is false.
    mode: pending.active ? 'updating'
      : reconnecting || socket?.connectionState !== 'connected' ? 'reconnecting'
      : 'retrying',
    sendNow: () => socket?.sendHeldNow(),
    discard: () => socket?.discardHeld(),
  }
}
