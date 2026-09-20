# A give-up path must name its subject, not just its label

## Symptom

`server.aio` logged `CRITICAL bounded_wait: phase 'stall.interrupt:<sid>' ignored
cancellation 2.0s after a 5.0s budget` 15 times over 11 days on two hosts (issue
#1539). The log names the phase and nothing else, so nobody could say what the task
was parked on. Every candidate absorber on that path — the SDK's
`_send_control_request` (`anyio.fail_after` + `Event.wait`), the LLM helper lane
(`run_helper_call`), and a SQLAlchemy/asyncpg query — honours an external
`Task.cancel()` within milliseconds when probed in isolation, so the message's own
claim ("ignored cancellation") could be neither confirmed nor located. Eleven days of
recurrence produced no diagnosis because the log carried no evidence.

## Anti-pattern

A bounded/timeout/circuit-break path that gives up and records only the **label** it
was given (a phase name, a subsystem string, an operation id) while discarding the
**subject** it gave up on — the live stack, the request, the task. The label was
chosen by the caller before the failure existed, so it can only ever restate what the
caller already knew. Two consequences compound it:

- The give-up log asserts a cause it did not measure ("ignored cancellation") when all
  it observed was "still pending at my deadline" — slow and stuck are conflated.
- The abandoned subject is dropped with no reference held, so its eventual outcome is
  never observed. In asyncio that also means a later exception surfaces through the
  default handler as an unattributed `Task exception was never retrieved`.

## Fix shape

Capture the subject **at the moment of giving up, while it is still parked**, and
follow it to its end:

- `bounded_wait` now logs `format_task_dump([task])` with the CRITICAL. That helper
  (and `iter_task_frames`) already existed in the same module, written for issue #846
  so "the next hang names its culprit", and `reap_lingering_db_tasks` already used it —
  `bounded_wait`, the module's primary abandonment path and the only one that fires in
  prod, did not. `Task.get_stack()` is not a substitute: it returns only the outermost
  suspended frame, which for any wrapped task names the wrapper.
- `_watch_abandoned(task, phase, level=...)` attaches a done callback that retrieves
  the outcome and reports it — `unwound` / `failed` / `completed N.Ns later — it was
  slow, not stuck`. This kills the unattributed-exception noise and makes the CRITICAL
  falsifiable: a recurrence whose companion line always says "completed" is a budget
  that is too small, not a wedge.
- The message states what was observed (`did not unwind within Xs of cancellation`)
  rather than a motive it cannot see (`ignored cancellation`).

Note the deliberate asymmetry with `cancel_and_reap`: it must NOT escalate with a
second cancel (issue #1164 — a second `cancel()` lands inside the child's teardown
region and cuts it short), and it stays at DEBUG because it is a shutdown path whose
contract is to be quiet. `_watch_abandoned` takes a `level` for exactly that, mirroring
`_consume`'s existing convention. Raising its volume would be a behaviour change
dressed as a diagnostic.

## Where to look / sibling audit

Grepping `server/` and `src/` for `abandon` / `gave up` / `giving up` outside tests
returns only `server/aio.py` and one comment in `server/app.py` referring to it: this
repo already funnels every "give up on an await" through the one module, so the sweep
found exactly one sibling — `cancel_and_reap`'s timeout branch, which dropped its task
unconsumed in the same way and now calls `_watch_abandoned` at DEBUG.
`reap_lingering_db_tasks` was already compliant (it dumps, and returns the leftovers to
its caller).

When auditing for this shape elsewhere, the tell is a log line whose entire payload is
an f-string the caller passed in. Ask: *could a reader locate the problem from this
line alone?* Candidates beyond asyncio: HTTP/RPC timeouts that log the endpoint but not
the in-flight request, circuit breakers that log the subsystem but not the failure that
tripped them, and watchdogs that log the session id but not the state they judged.

Test hygiene the same shape imposes (`server/tests/test_aio_bounded_wait_abandon.py`):
a fixture that parks a cancel-immune task must release it in a `finally` **on the
running loop**. A failing assertion that skips an inline release leaves the task
parked, and the loop's own `_cancel_all_tasks` — which delivers exactly one cancel and
then gathers unboundedly — wedges, so the suite hangs instead of reporting the failure.
A sync pytest fixture cannot do this: its teardown runs after the loop has closed.
