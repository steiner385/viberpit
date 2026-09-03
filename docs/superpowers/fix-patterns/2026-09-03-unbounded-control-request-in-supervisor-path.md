# Fix pattern: an unbounded SDK control request inside a supervisor path

**Date:** 2026-09-03
**Origin:** RCCA of the "session stuck at usage limit; auto-rotation and manual account switch
both fail" incident (production session `888ad2bf`, dobby, 2026-09-03 13:14–13:33 EDT). One
instance is fixed here; the sibling sweep below is the one `CLAUDE.md`'s "audit recurring fix
patterns" rule requires.

## Symptom

- The session's `claude` CLI emits "You've hit your session limit · resets 3:20pm" and goes
  silent. The card flips to **Needs attention / Turn failed**.
- The journal shows `reassign.error session=… Exception: Control request timeout: interrupt`
  followed by `recover.paused … reason=reassign_failed:<reason>` **once a minute**, with
  `<reason>` cycling through `session limit reached`, `stuck_session_sweep`, and — when the user
  tries the account dropdown — `manual`. The edge logs `ws.command failed … name=account
  DaemonTimeout` because its 30 s RPC budget expires before the daemon's 60 s one.
- `ps` shows the session's CLI alive and idle (`S`, `ep_poll`), i.e. not crashed — it has just
  stopped answering the SDK control channel.

## Anti-pattern

`SessionOrchestrator._reassign` — the shared primitive under usage-ceiling rotation, crash /
401 / 429 recovery, the stuck-session sweep, stall recompose, AND the user's manual switch —
opened with:

```python
if old_driver is not None:
    await old_driver.interrupt()      # unbounded
    await old_driver.aclose()
```

`SessionDriver.interrupt()` awaits `ClaudeSDKClient.interrupt()`, which is
`Query._send_control_request(...)`: write a JSON line to the CLI's stdin and wait up to **60 s**
for the matching `control_response`. On timeout the SDK raises a **plain `Exception`**, not
`CLIConnectionError` — so the `except CLIConnectionError` inside `driver.interrupt()` did not
catch it, `_reassign`'s `except Exception` did, and the method exited via `_pause_session`
**before `aclose()` and before the relaunch**. The wedged driver stayed installed in
`_drivers`; every retry path captured the same driver and died the same way. The system had
correctly decided to rotate 16 times and could not execute the decision once.

Two properties make the shape dangerous:

1. **A courtesy step gated the essential step.** The interrupt is a nicety for the driver about
   to be *discarded*; the relaunch is the fix. The nicety's failure mode (a CLI that ignores the
   control channel) is exactly the situation in which the relaunch matters most.
2. **The SDK's error type is not the one the caller was hardened against.** `aclose()` had
   already been made safe for this in #846 with `bounded_wait(self._client.interrupt(), …)`; the
   explicit pre-close interrupt in `_reassign` was the un-hardened sibling of the same call.

Also observed: killing the wedged CLI by hand did **not** immediately clear the state. Its stdin
/ stdout pipes were still held by a child (`bash` tool subprocess), so the SDK reader saw no EOF
and `transport.write()` kept succeeding — the next reassign timed out identically. Only once the
SDK got `exit code 143` did the driver relaunch. "The process is dead" is not the same as "the
SDK knows it is dead".

## Fix shape

- **Bound the courtesy, never let it gate the essential.**
  ```python
  await bounded_wait(old_driver.interrupt(), timeout=_CLOSE_GRACE_SEC,
                     phase=f"reassign.interrupt:{record.id}")
  await old_driver.aclose()
  ```
  `server.aio.bounded_wait` never raises and never hangs (cancels after `timeout`, abandons after
  `cancel_grace`, logs either). `_CLOSE_GRACE_SEC` is the same 5 s grace `aclose()` uses, imported
  from `server.sessions.driver` rather than duplicated.
- **Test the failure mode, not the happy path**: `server/tests/test_reassign_interrupt_timeout.py`
  drives `_reassign` with an old driver whose `interrupt()` (a) raises the SDK's plain `Exception`
  and (b) never returns, and asserts `aclose()` ran, the new driver started, `record.account`
  moved, a `rotated` event was published and `_pause_session` was **not** called.

## Sweep: every `await <driver>.interrupt()` in `server/sessions/orchestrator.py`

| Site | Path | Verdict |
|---|---|---|
| `_reassign` | rotation / recovery / sweep / manual switch — driver is being replaced | **Fixed** (this change) |
| `_check_stalls` | stall watchdog tick, loops over *all* drivers | **Fixed** — an unbounded interrupt on one wedged CLI blocked the whole tick for 60 s and skipped every later driver's stall check and the disabled-account sweep for that tick. Bounded the same way; the stall-cap → `stall_recompose` escalation (which lands in the now-safe `_reassign`) is unchanged. |
| `interrupt(session_id)` | user pressed Stop | Left as is — the user *wants* the CLI's answer; a bounded no-op would report success for a Stop that did nothing. The wedge still surfaces as the 60 s error + the stall watchdog escalation. Candidate for a shorter explicit timeout + a "CLI unresponsive" event. |
| `set_model_for_session(when="now")` | mid-turn model swap on the same driver | Left as is — if the interrupt fails, resending the prompt would queue behind a turn that never stopped; raising to the caller is the honest outcome. Same follow-up shape as Stop. |

`SessionDriver.aclose()` already bounds its own interrupt (#846) and was not changed.

## Recognise it next time

Any `await sdk_client.<control-op>()` (`interrupt`, `set_model`, `set_permission_mode`,
`get_server_info`, …) inside a **supervisor** path — anything that runs unattended to recover,
rotate, close or restart a driver — must be wrapped in `bounded_wait` unless its failure is
*meant* to abort the supervisor. The SDK's control-request timeout is a plain `Exception`;
`except CLIConnectionError` does not cover it.
