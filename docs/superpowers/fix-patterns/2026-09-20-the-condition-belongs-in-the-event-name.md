# The condition belongs in the event name

## Symptom

An auto-error issue that never closes and never explains itself. #1857 —
`codex.manager.runner_crashed: account=<str> generation=<n> exit_code=<n>
reason=<str>` — collected 42 occurrences over nine days on dobby. Every field
that would have said *which* failure this was is `<n>` or `<str>` in the title,
and the record carried no post-mortem at all. Nine days of recurrence comments
and nobody could say what had happened, let alone whether the same thing had
happened twice.

## Anti-pattern

**Putting the condition in a field and the name in the event.** The error sink
fingerprints a structured record on `(source, level, logger, event, exception,
frame)` and *never* on its fields (`server/obs/error_signature.py:167`), and it
normalizes every number in the title to `<n>`. So a field is not a discriminator:
it is invisible to the thing deciding what counts as one issue.

`_handle_exit` logged one event for three unrelated endings —

- the #1444 shape: the client latched closed while the process was still alive,
- a non-zero status or a signal: the app-server died,
- `exit_code == 0`: the app-server exited **by itself**, unasked, with nothing in
  flight and a replacement serving 1.4 s later,

— which made them one issue whose occurrence count was dominated by the third.
A genuine crash would have arrived as "1 more occurrence" of a condition that had
already been read and dismissed 42 times.

**Computing the diagnosis and then dropping it.** The stderr tail *was* read
(`runner.process.stderr_tail()`), handed to the in-memory `RunnerNotice`, and
left out of the log line — so the one record that outlives the process was the
one without the evidence.

## Fix shape

Two axes, and they are not the same axis.

- **The condition goes in the event name.** `classify_runner_ending()`
  (`server/codex/manager.py`) maps the exit code to a `RunnerEnding` and its own
  event: `runner_crashed`, `runner_client_closed`, `runner_exited`. One classifier
  so the name, the reason string and the level cannot drift apart.
- **The cost goes in the level.** A lost turn is ERROR whatever ended the runner;
  an ending with nothing in flight is ERROR only when the ending is itself a
  fault. The same clean exit is therefore WARNING while idle and ERROR when it
  interrupted a turn — two fingerprints, because level is part of the sink's key,
  so "the runner left mid-turn" can never hide inside "the runner left".
- **Split, but no further.** Two crashes with different exit statuses stay one
  issue. The guard asserts both halves at the real `signature_of`, not against
  hardcoded names — collapse two endings back onto one name and it goes red.
- **Carry the post-mortem**: `detail`, `uptime_s`, `pid`. `detail` goes *last*
  because the issue title is the field summary clipped to 100 characters, and a
  leading stderr tail evicts the fields triage reads.

Recovery is untouched: all three endings still notify, close the turn book,
discard and restart. Only the reporting differs.

## Where to look

- Fixed: `_handle_exit` / `classify_runner_ending` in `server/codex/manager.py`.
- Same shape, being fixed in parallel: `recover.paused`'s reason field (#1780,
  PR #2128) — one event name, N reasons, the count sized by a retry cadence.
- **Already correct, do not "fix":** the `error=scrub_exception(exc)` field on
  `codex.driver.resync_failed`, `codex.protocol.*_failed` and friends (#1861).
  There the event already names the condition and the field is a *detail* of it.
  The test is not "does a field vary" but "would a reader want these as separate
  issues, and does one of them need a different severity".
- Reviewed and left alone: `codex.protocol.malformed_frame` and
  `codex.external_auth.degraded` both carry a `reason` spanning variants of one
  condition with one fix; splitting them would file issues nobody would triage
  differently.
- Companion: `2026-09-20-a-refusal-is-a-state-not-an-event.md` is about *how
  often* to record a fact. This one is about *which fact* the record names.
