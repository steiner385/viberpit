/**
 * Per-channel socket open flags, held apart from `ws/client.ts`.
 *
 * `api/hooks.ts` needs "is any WS feeding the sessions cache?" for the FR-403 trust order
 * in `useSessions` (an open socket's WS patch outranks the poll; the poll outranks a closed
 * socket), but `ws/client.ts` imports `QK` from `api/hooks.ts`, so reading
 * `getSocket().connectionState` there would close an import cycle. Every channel counts:
 * each `SessionSocket` that `useSessionSocket(channel)` wires a QueryClient onto patches
 * `QK.sessions` from its own `_handleInbound`, and channels reconnect on independent
 * backoff, so the route socket being down does not mean the cache has gone unrefreshed.
 * The socket registry in `client.ts` is the only writer: it sets a channel's flag from its
 * `connectionStateChange` events and clears it when it drops that socket (destroy emits no
 * state change).
 */
import type { SocketChannel } from './client'

const open: Partial<Record<SocketChannel, boolean>> = {}

export function setChannelOpen(channel: SocketChannel, isOpen: boolean): void {
  open[channel] = isOpen
}

/** True while at least one channel's socket is `connected`. */
export function isAnySocketOpen(): boolean {
  return Object.values(open).some(Boolean)
}
