# Spike: do finished sessions still hold a live driver?

**Date:** 2026-08-23
**Question:** Task 2 wants to stop `complete`/`done` sessions from consuming a capacity slot.
That's only safe if those sessions hold no live driver / `claude` process. This spike checks.

## tl;dr

**Ambiguous — recommend STOP.** The code is unambiguous that reaching `complete`/`done`/`idle`
does **not** tear down the session's driver or its underlying `claude` process — only
`close_session`, `archive_session`, or full daemon `stop()` do that, and archive is permanently
blocked for 6 of the 7 stuck sessions by `auto_archive_opt_out_reason='manual-unarchive'`. The
live daemon currently shows zero drivers and zero session processes for these sessions, but that
reading is confounded by a daemon restart 46 minutes before this check, which wipes in-memory
driver state without recreating it (`rehydrate()` restores DB records, not drivers). The current
"all clear" is therefore not proof that finished sessions are safe to exclude from the capacity
cap in the general case — under long-running (non-restarted) operation, the code indicates they
would still hold a live driver/process.

## Step 1 — code: does reaching complete/done pop the driver?

`server/sessions/orchestrator.py` has exactly three `self._drivers.pop(...)` sites, plus one more
found while reading (`archive_session`, not called out in the brief but directly relevant):

- **L788**, inside `stop()` (full daemon shutdown) — closes every driver unconditionally.
- **L1237**, inside `close_session()` — explicit user/API-triggered close.
- **L6466**, inside `archive_session()` — "mirrors close_session head."

None of these run automatically when a session's status transitions to `complete`, `done`, or
`idle`. That transition goes through `_persist_status()` (L1325-1433), the driver's `on_status`
callback. Reading it end to end: it updates `rec.status`, the DB row, pushes a WS event, runs
`_maybe_apply_auto_name`, and calls `_maybe_autoarchive_completed(session_id, status, rec)`
(L1433) — **it never touches `self._drivers`.**

`_maybe_autoarchive_completed` (L1439-1465) is the *only* path from a status transition to a
driver teardown, and only for `status == "complete"`:

```python
if status != "complete":
    return
if rec is None:
    return
if getattr(rec, "auto_archive_opt_out_reason", None):
    return          # <-- sticky opt-out always wins, per its own docstring
...
self._auto_archiving.add(session_id)
asyncio.create_task(self._auto_archive_completed(session_id), ...)
```

So: a `done` or `idle` session's driver is **never** popped by reaching that status — full stop.
A `complete` session's driver is popped only if auto-archive fires, which requires no opt-out.
6 of the 7 stuck sessions carry `auto_archive_opt_out_reason='manual-unarchive'`, permanently
blocking this path; the 7th (`done`) never goes through this branch at all (it only checks
`complete`).

**Underlying process lifetime, for context:** `server/sessions/driver.py`'s `start()` builds
`self._client` once via the SDK factory and enters it (`__aenter__`); `send_prompt`/`_start_turn`
reuse that same open client across turns via `.query()` — the client (and its underlying `claude`
subprocess, spawned by `claude_agent_sdk`) is not re-spawned per turn and is not torn down when a
turn ends. It's only closed in `driver.aclose()`, called from the same three orchestrator sites
above. So the code's design intent is for the process to persist idly after a turn completes,
ready for the next prompt — consistent with the driver-retention finding.

**Conclusion from code alone:** finished (`complete`/`done`/`idle`) sessions **do** retain their
driver entry and (by extension) their `claude` subprocess, indefinitely, unless explicitly closed
or archived. No idle-timeout / driver-janitor mechanism exists in `orchestrator.py` to age one out
(searched for `INACTIVE_SESSION_STATUSES` — confirmed `frozenset({"ended", "archived"})` only,
not `idle`/`complete`/`done`; and the only periodic janitor, `_janitor_sweep_once`, only sweeps
orphaned git worktrees, not drivers).

## Step 2 — the live daemon (rocky)

```
$ curl .../info
{"sha":"f82d230","protocol":1,"epoch":"1a02da070ba-3968104-6091a467",
 "loadAvg":0.75,"memAvailableMb":26847,"activeSessions":0}

$ curl -X POST .../rpc/live_session_counts -d '{}'
{"result":{}}
```

`live_session_counts` iterates `self._drivers.keys()` and buckets by account/status
(orchestrator.py:4820-4837). An empty `{}` means **zero entries in `_drivers`, for any account,
right now** — not just for the 7 stuck sessions. `activeSessions: 0` in `/info` corroborates.

Cross-checked against `/api/sessions` on dobby (`http://dobby.tail0dd570.ts.net:8787/api/sessions`):
status counts are `{'queued': 15, 'idle': 3, 'complete': 3, 'done': 1}` — exactly the 7
finished + 15 queued described in the incident. IDs for the 7 finished sessions:

```
521e7ca0 idle, 039ca589 idle, b3f6117b idle,
d9e93f4b done,
e4d0d620 complete, ecdaa2a8 complete, 96706a1b complete
```

**Confound:** `ccswitch-daemon.service` on rocky shows `Active: active (running) since
2026-08-23 03:58:03 EDT; 46min ago` (checked at ~04:47 EDT) — i.e. the daemon restarted well
after the queue-deadlock incident began (2026-08-22) and only 46 minutes before this check.
`_drivers` is a plain in-memory dict; `rehydrate()` (orchestrator.py ~L3900-3950) rebuilds
`self._records` from the DB on startup but never touches `self._drivers` — a driver is only
(re)created lazily, on next attach or `send_prompt`, via the `_build`/`driver.start()` path at
orchestrator.py:4960-5010. None of the 7 finished sessions has been attached to or prompted since
the restart (they're finished — nothing is driving them), so no driver has been lazily recreated
for them. **The empty `_drivers` reading is consistent with either "these sessions never keep a
driver past completion" or "the restart wiped it and nobody's touched them since" — it cannot
distinguish the two.**

## Step 3 — surviving processes on rocky

```
$ ssh tony@rocky 'pgrep -af "claude"'
360112 sshfs ... tony@100.102.11.121:/home/tony/.claude-accounts /home/tony/.claude-accounts
360121 sshfs ... tony@100.102.11.121:/home/tony/.claude /home/tony/.claude
4122204 .../claude_agent_sdk/_bundled/claude --output-format stream-json --verbose
         --system-prompt "You name coding sessions. ..." --model haiku
         --setting-sources= --input-format stream-json
```

Read carefully, as instructed:
- The two `sshfs` lines are incidental substring matches (mount paths containing `.claude` /
  `.claude-accounts`) — not `claude` processes at all. **0 genuine matches here.**
- PID 4122204 *is* a genuine `claude_agent_sdk`-spawned process, but its `--system-prompt` is the
  literal auto-titling prompt from `server/sessions/title_generator.py:21` ("You name coding
  sessions...", `--model haiku`) — a one-shot title-generation subprocess
  (`_maybe_apply_auto_name`), not an interactive session driver. `pstree -p 3968104` confirms it's
  the only `claude` child of the daemon PID, with a cluster of worker threads under it, no other
  session-shaped children.

**Genuine live session-driver processes right now: 0.** This matches Step 2's empty `_drivers`.

## Per-status finding

| status     | driver retained by code? | driver retained live (now)? | process alive (now)? | evidence |
|------------|---------------------------|------------------------------|------------------------|----------|
| `idle`     | Yes (never popped)        | No                            | No                     | `_persist_status` never touches `_drivers`; live `_drivers` == `{}`; pgrep found 0 session processes |
| `complete` | Yes, unless auto-archived — blocked here by `auto_archive_opt_out_reason` | No | No | `_maybe_autoarchive_completed` early-returns on the opt-out; live `_drivers` == `{}` |
| `done`     | Yes (never popped; `_maybe_autoarchive_completed` only checks `complete`) | No | No | same as idle |

Code and live-daemon evidence **disagree** on whether finished sessions hold a driver in the
general case — reconciled above as a restart confound, not a contradiction in the code's logic.

## Recommendation

**STOP.** Do not treat the current "zero drivers, zero processes" reading as proof that
`complete`/`done`/`idle` sessions are safe to exclude from the capacity cap in general. The code
shows driver teardown is tied to `close_session`/`archive_session`/daemon `stop()`, not to status.
Under normal (non-restarted) operation — the common case — a session that reaches `complete` or
`done` keeps its driver and underlying `claude` process alive indefinitely unless something
explicitly closes or archives it, and for 6 of these 7 sessions archival is permanently blocked by
the sticky manual-unarchive opt-out. The only reason today's snapshot shows zero live drivers is
that the daemon restarted 46 minutes ago and nothing has re-touched these particular sessions
since. Task 2 as scoped (change the capacity-counting predicate to exclude `complete`/`done` by
status alone) risks oversubscribing the host the next time the daemon runs for an extended period
without restarting and a session reaches `complete`/`done` while still driver-backed. This needs
to go back to the controller for a decision — a status-only capacity fix is not what the evidence
supports; the safe predicate would need to also confirm no live driver (e.g. check
`session_id not in self._drivers`), or a driver-teardown task should run first. Not improvising
that fix here per the brief's instruction — flagging it for the controller instead.
