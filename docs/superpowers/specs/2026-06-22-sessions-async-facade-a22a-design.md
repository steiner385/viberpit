# Session daemon — A2.2a: async orchestrator facade (in-process)

**Date:** 2026-06-22
**Status:** Approved (design)
**Phase:** A2.2a. A2.2 (full breadth + edge cutover) is split A2.2a (this — unify the edge's
orchestrator interface to fully-async, in-process only) → A2.2b (complete `RemoteOrchestrator`,
`app.py` remote flag, WS-bridge `subscribe()` branch, remote-mode proof). Builds on A2.1.

## Why

A `RemoteOrchestrator` must be **all-async** (every call is RPC). But the edge calls the
in-process orchestrator **synchronously** in ~10 places (`get_session`, `list_sessions`,
`subscribe_events`/`unsubscribe_events`, `transcript_path`, `pending_questions`,
`pop_pending_initial_prompt`/`_images`, `resolve_permission`, `answer_question`,
`command_catalog`) and reaches into `orch.poller` for usage. For one set of edge call sites
to work against both the in-process orchestrator and the remote proxy, the edge must talk to
a **uniform async interface**.

A2.2a introduces that interface as a thin async facade over the unchanged real orchestrator,
and repoints the edge call sites at it. No remote, no behavior change — purely the interface
unification, with the existing REST+WS suites as the safety net. This de-risks the refactor
on its own before A2.2b swaps in the remote implementation.

Chosen over async-ifying the real orchestrator's methods directly: ~600 existing tests call
those methods synchronously, so async-ifying them would force massive test churn. The facade
keeps the real orchestrator (and its tests) untouched.

## Design

### The boundary: a second reference, `app.state.sessions`

`app.state.orchestrator` stays the **raw** `SessionOrchestrator` — the in-process lifecycle
owner the lifespan drives (`rehydrate`/`start`/`stop`) and Phase-B healthz reads
(`active_session_count`). A new **`app.state.sessions`** holds the async facade for session
*operations*, which the API/WS/accounts layers use. Keeping them separate makes A2.2b a clean
swap: `app.state.sessions` becomes a `RemoteOrchestrator` and the edge simply skips the
lifecycle calls (the daemon owns them) — without disturbing the lifespan or the many tests
that read `app.state.orchestrator` directly.

### Components

- **`InProcessOrchestratorAdapter`** (`server/sessions/facade.py`, new): constructed with a
  real `SessionOrchestrator`; exposes **async** methods for every operation the edge calls.
  - Already-async orchestrator methods → `return await orch.<m>(...)`: `create_session`,
    `send_prompt`, `ensure_driver`, `archive_session`, `unarchive`, `permanent_delete`,
    `adopt_session`, `rename_session`, `list_archived`, `transcript_events`, `interrupt`,
    `set_permission_mode`, `cancel_queued`, `run_command`.
  - Currently-sync orchestrator methods → trivial `async` wrappers returning `orch.<m>(...)`:
    `list_sessions`, `get_session`, `transcript_path`, `pending_questions`,
    `pop_pending_initial_prompt`, `pop_pending_initial_attachment_ids`, `resolve_permission`,
    `answer_question`, `command_catalog`, `subscribe_events`, `unsubscribe_events`.
  - `async def usage(self, name) -> ...`: wraps `orch.poller.get(name)` (so the edge stops
    reaching into `.poller`, which won't exist remotely).
  - `async def pending_permissions(self, session_id) -> ...`: wraps the one private-internal
    access the WS bridge makes today (`orch._registry.pending(session_id)` at `ws.py:199`), so
    the edge no longer reaches into `orch._registry` — the facade owns that boundary.
  - `ensure_driver` returns the real `SessionDriver | None` (the WS bridge's in-process
    `driver.events()` path is unchanged; the remote bool+`subscribe()` divergence is A2.2b).
  - Signatures/return types mirror the underlying orchestrator methods exactly (just async).

- **Edge call-site repointing**: in `server/api/*` and `server/bridge/ws.py`, the ~10 sync
  calls become `await app.state.sessions.<m>(...)`; the already-awaited calls repoint from the
  orchestrator to `app.state.sessions`; `accounts.py`'s `await orch.poller.get(name)` →
  `await sessions.usage(name)`. (`subscribe_events` returns the same `asyncio.Queue`; the
  caller just awaits the getter.)

- **`server/app.py`**: after building the raw `orchestrator`, set
  `app.state.sessions = InProcessOrchestratorAdapter(orchestrator)`, and pass that facade to
  `build_ws_router(...)`. The lifespan is unchanged (still drives `app.state.orchestrator`).

### Data flow

Unchanged at runtime — same orchestrator, same behavior. The facade is a thin async
pass-through; its only purpose is to present the uniform async interface A2.2b's remote proxy
will satisfy.

### Error handling

None new. The adapter does not catch anything — it is a pass-through (an underlying exception
propagates exactly as today). Remote-specific `DaemonUnavailable`→503 mapping is A2.2b.

## Testing

- **Regression (the real proof):** the existing REST + WS test suites stay green with the edge
  routed through the facade (run in the default in-process mode). This proves the repointing is
  behavior-preserving.
- **Adapter unit test:** for a sampling across the three kinds — an async method
  (`send_prompt`), a formerly-sync method (`get_session`), and `usage` — assert the adapter
  awaits/returns the underlying orchestrator's result (using a mock orchestrator), and that the
  formerly-sync wrappers are awaitable and return the orchestrator's value unchanged.

## Out of scope (A2.2b and later)

- `RemoteOrchestrator` completing the same interface over RPC; the daemon-side RPC handlers for
  the remaining methods; `poller`/`usage` over RPC.
- The `app.py` `CCSWITCH_SESSIOND_SOCKET` flag selecting the remote facade.
- The WS-bridge remote branch (`ensure_driver`→bool + `subscribe()` for live events).
- Running the suites in remote mode.
- Any change to the real `SessionOrchestrator` (it stays sync/async exactly as today).
