# Performance Notes

## WS output batching (NFR-005)

Terminal output from each PTY subprocess arrives as a continuous byte stream.  Rather than
forwarding every byte (or every `read()` chunk) as a separate WebSocket frame, the
`_batch_output` coroutine in `server/bridge/ws.py` coalesces chunks over a ~16 ms window
(matching a 60 fps display refresh cadence, `_BATCH_INTERVAL = 0.016`).  The batcher drains
all items already in the per-session queue in a tight `get_nowait()` loop, concatenates them
into a single base64-encoded `output` frame, then waits up to one interval for the next chunk.
Under a burst this collapses N input chunks into O(1) frames; under a slow stream (chunks spaced
further apart than the interval) each chunk flushes independently with no added latency beyond the
interval.  The coalescing invariant and data-integrity guarantee are verified by the six
`test_backpressure.py` tests (which drive the same logic path used in production).

## Scrollback ring (NFR-003)

Each session maintains a bounded in-memory scrollback ring capped at **256 KB** of raw PTY bytes
(`SessionManager`, `max_scrollback_bytes`).  This bounds per-session memory to a fixed ceiling
regardless of session lifetime or prompt verbosity, while still providing enough history for a
WebSocket reconnect to replay meaningful context (SC-008: ≤ 10 s reconnect with full recent
output).

## Usage polling cadence

Account usage is polled via `GET /api/oauth/usage` (the authoritative Anthropic endpoint) on a
**60-second interval** per account (`SessionOrchestrator`, `poll_interval`).  This cadence trades
freshness for API-rate courtesy; the displayed percentages are therefore ≤ 60 s stale.  The
WebSocket usage-push path forwards the latest cached value immediately on connect and after each
successful poll.

## Concurrency expectation

The server is designed for **~10 concurrent sessions** on a personal-use Tailscale deployment
(one operator, a phone + laptop on the same tailnet).  Each session holds one PTY file
descriptor, one `asyncio.Queue`, one scrollback buffer (≤ 256 KB), and a pair of coroutine
tasks (relay + batcher).  At 10 sessions the total overhead is modest (<5 MB memory, <5% CPU
idle).  Scaling beyond ~50 concurrent sessions would require profiling the anyio scheduler and
potentially moving PTY I/O to a thread pool; that is out of scope for v1.
