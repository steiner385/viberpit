# WebSocket audit assertions need handler completion

## Symptom

The external-token permission test intermittently reads no `permission.allow`
audit row after receiving `turnEnd` and closing its TestClient socket. Under
full-suite load, teardown can cancel the audit transaction during commit.

## Anti-pattern

Treating a driver event from an independent stream as acknowledgement that the
WebSocket receive handler has finished its database transaction.

## Fix shape

Keep the socket open until the inline permission handler finishes. Send an
unknown frame after `turnEnd` and require its exact `UNKNOWN_TYPE` response,
reusing the sentinel pattern in `test_ws_commands.py`. The receive loop cannot
handle that frame until the preceding permission transaction exits. Then assert
the real persisted audit row; do not replace the audit writer or sleep.

A temporary experiment held the permission action after real audit insertion
until the sentinel was sent. The unchanged base test failed its audit assertion;
the corrected test passed. The experiment is not part of the shipped fixture.

## Where to look

Search tests using both `websocket_connect` and `AuditEvent`/`_read_audit`.
The current sweep found only `test_ws_external_token.py`. Ordinary driver-event
tests do not assert audit durability and need no matching change. This barrier
depends on `permissionResponse` remaining inline in `server/bridge/ws.py`; a
future deferred handler needs its own completion acknowledgement instead.
