# Contract: WebSocket (terminal I/O + control/event channel)

Single authenticated WebSocket per client (R7). Identity verified on upgrade (R5). Messages are
JSON envelopes except raw terminal bytes, which are sent as binary frames tagged by a preceding
`attach`. Channel multiplexes by `sessionId`.

## Client → Server
| type | payload | meaning |
|------|---------|---------|
| `attach` | `{ sessionId }` | focus a session; server replays recent scrollback then streams live |
| `detach` | `{ sessionId }` | stop streaming that session (it keeps running) |
| `input` | `{ sessionId, data }` | keystrokes/text to the PTY |
| `resize` | `{ sessionId, cols, rows }` | propagated to the PTY (`TIOCSWINSZ`) |
| `interrupt` | `{ sessionId }` | send interrupt to the session (user-initiated) |

## Server → Client
| type | payload | meaning |
|------|---------|---------|
| `output` | `{ sessionId, data }` | PTY output (live or replayed scrollback) |
| `replayComplete` | `{ sessionId }` | scrollback replay done; live output follows |
| `status` | `{ sessionId, status }` | session status change (≤3s, FR-019) |
| `usage` | `{ account, session:{pct,resetsAt}, weekly:{pct,resetsAt} }` | usage update (≤60s stale) |
| `rotated` | `{ sessionId, fromAccount, toAccount }` | auto-rotation occurred (FR-013); UI updates badge + may notify |
| `sessionEnded` | `{ sessionId, code }` | the session's process exited |
| `error` | `{ sessionId?, code, message }` | redacted of secrets |

## Behavior
- On reconnect, the client re-sends `attach`; the server replays the bounded scrollback (≤10s,
  SC-008) then resumes live.
- On `rotated`, the server has already rebridged the new PTY under the same `sessionId`; the client
  keeps its terminal and receives a forced repaint via the new `output` stream.
- Backpressure: server batches `output` frames (≥16ms cadence) and applies flow control if the
  send buffer grows, to protect mobile links (NFR-005).
- No frame ever carries token/credential material.
