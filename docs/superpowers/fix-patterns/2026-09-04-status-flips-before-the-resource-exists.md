# Fix pattern: a record's status flips before the resource behind it exists

**Date:** 2026-09-04
**Origin:** "new sessions don't actually start until I navigate away, then back into them"
(production sessions `bcbdf359` on rocky and `512a8b0a` on dobby, 2026-09-04 00:50–00:52 EDT,
right after #1028 deployed — every Fable session approved from its open view). One instance
is fixed here; the sibling sweep below is the one `CLAUDE.md`'s "audit recurring fix
patterns" rule requires.

## Symptom

- A session launched with a gated model (Fable → `pending_approval`) or into a full slot
  (`queued`) is opened, then approved / dequeued. It shows **Ready**, the launch prompt never
  runs, and nothing arrives. Navigating away and back starts it instantly.
- Edge journal (`ccswitch.service` on dobby), per session:

  | +ms | line |
  |---|---|
  | 0 | `POST /api/sessions` 201, `pending_approval` |
  | 40 | `ws.attach.decision … detached: true` — expected, no driver yet |
  | ~3300 | `POST …/model-approval` starts (2.7 s) |
  | 5200 | `GET /api/sessions` (5 s poll) — status already `idle` |
  | 5220 | `ws.attach.decision … detached: true` ← the view's re-attach, **mid-spawn** |
  | 6000 | approval returns; driver registered; nobody re-attaches |
  | 27000 | user navigates back → `detached: false` → `sending deferred initial prompt` |

- rocky's daemon logs nothing, so the edge's `ws.attach.decision` line is the only signal.

## 5 whys

1. The launch prompt never ran because it is DEFERRED until the first **live** WS attach
   (`_pending_initial_prompt`, popped in `ws.py::_send_deferred_initial_prompt`), and the
   only attach after the spawn took the read-only *detached* path, which never pops it.
2. The bridge took the detached path because `get_session()` reported `detached=True`:
   `_sync_detached` derived it purely from `_drivers` membership.
3. There was no driver yet because `_spawn_session` **pre-seeds the record as `idle` before
   `driver.start()`** and registers the driver only when `start()` returns (1–3 s: the SDK
   subprocess + its MCP fleet). For that window the record reads "spawned, but detached" —
   indistinguishable from a finished session.
4. The client re-attached inside that window because its pre-spawn→spawned trigger is the
   record's *status* (the 5 s list poll saw `pending_approval` → `idle` at the pre-seed),
   and the status never changes again when the driver lands, so nothing re-fires.
5. A second hole made it worse: `resolve_model_approval` and `_maybe_start_queued` **popped
   the record** before calling `_spawn_session`, which re-seeds it after worktree checkout,
   config compose and X-display setup. A poll in that gap saw the session *vanish*; the
   client's transition detector (`prev.status === undefined → return`) then never fired at all.

## Anti-pattern

```python
self._records[session_id] = SessionRecord(..., status="idle")   # visible NOW
...
await driver.start()                                             # seconds
self._drivers[session_id] = driver                               # the resource lands HERE

def _sync_detached(self, rec):
    rec.detached = rec.id not in self._drivers                   # ← mid-spawn == detached
```

A derived flag reads *resource presence* while the resource is still being constructed, and
the constructing path has already published the "done" status. Every consumer that branches
on the flag (the WS bridge, `ensure_driver`'s "no driver → build one") misclassifies the
in-flight state as the terminal one. The client cannot fix this from outside: it observes
the status, and the status told the truth too early.

## Fix shape

- **Track the in-flight construction** next to the resource map: `_spawning[session_id]`
  is a future registered at the pre-seed and resolved in `_spawn_session`'s `finally`.
- **The derived flag knows about it**: `detached = no driver AND not spawning`.
- **The "build one" path waits instead of racing**: `ensure_driver` awaits the spawn (bounded
  by `SPAWN_WAIT_S`) and returns *its* driver. Before, a mid-spawn `ensure_driver` built a
  second driver with `resume=None` — a second SDK process on a brand-new conversation,
  silently overwritten by the spawn's own registration.
- **Never make the record vanish across the construction**: the approval and queue paths
  leave the record in place; `_spawn_session` overwrites it (and keeps its
  `created_at`/`host`, as its own comment already promised). Both statuses are
  capacity-exempt, so the cap check is unchanged.
- Tests: `server/tests/test_attach_during_spawn.py` gates the fake SDK's connect so the
  spawn is held mid-flight, then attaches / calls `ensure_driver` / snapshots
  `get_session` inside the window.

## Where to look (sibling sweep, 2026-09-04)

| Site | Verdict |
|---|---|
| `_spawn_session` pre-seed → `_drivers` registration | **fixed here** (`_spawning`) |
| `resolve_model_approval` record pop before spawn | **fixed here** (record stays) |
| `_maybe_start_queued` record pop before spawn | **fixed here** (record stays) |
| `ensure_driver` build path (resume) | safe — registers under `_ensure_lock`; the record is *already* detached, no status flip |
| `_reassign` (rotation / manual switch) | safe — reads the old driver with `get`, keeps it registered until `_install_driver` swaps in the started replacement; no detached window |
| `_persist_status` completion release (`_releasing`) | safe — the driver leaves `_drivers` only once the session is `complete`, and `ensure_driver` already awaits `_releasing` |

Watch for the shape in anything that publishes a status/record *before* awaiting the thing
that status implies exists, and in any flag derived from "is X in this dict".

## Reference

- Timeline above is from `journalctl -u ccswitch.service` on dobby, 2026-09-04
  04:50:17–04:50:44 UTC, session `bcbdf359`.
- Operator workaround before the fix deploys: navigate away from the session and back (a
  fresh attach after the driver landed pops the deferred prompt).
