# Fix pattern: a slug in a throttled record splits one condition into N issues

**Date:** 2026-09-20
**Origin:** #1766 / #1780 / #1843 — three auto-error issues filed by one `log_throttled`
call in `SessionOrchestrator._pause_session`. One instance is fixed here; the sibling
sweep below is what `CLAUDE.md`'s "audit recurring fix patterns" rule requires, and is
tracked separately because each remaining call site changes a different suite.

## Symptom

- Several open `[auto-error]` issues whose titles differ only in one word, filed against
  the same logger and the same function: `recover.paused session=<id>
  reason=reassign_failed:stuck_session_sweep` (#1766), `… reason=reassign_failed:weekly
  limit reached` (#1780), `… reason=rotation_cap` (#1843).
- Occurrence counts far above what the record's `LogThrottle` window allows. #1780 logged
  161 records in 2.8 h for session `331f7d98` against a 6-hour window that caps one key at
  four records a day.
- Closing one issue does not quiet the journal, because the *next* value of the slug files
  a fresh lineage under a fresh fingerprint.

## Anti-pattern

Two independent defects that travel together, because throttling and identity are separate
halves of the same problem and a call site usually gets both wrong at once.

**Half one — identity.** `server/obs/error_signature.py:signature_of` fingerprints a plain
record on `normalize(record.getMessage())`, and `normalize()` only masks the shapes it
recognizes: uuids, quoted strings, hex runs, ints. A slug — a reason, a repo, an account
name, a host — has none of those, so it survives into the fingerprint:

```python
log_throttled(log, _PAUSE_LOG, f"{record.id}:{reason}", logging.WARNING,
              "recover.paused session=%s reason=%s", record.id, reason)
#                                        ^ masked      ^ NOT masked → one issue per value
```

**Half two — pacing.** `_pause_session` re-armed the window whenever the reason changed:

```python
if previous_reason != reason:
    _PAUSE_LOG.forget(f"{record.id}:{reason}")
```

That is a no-op for its stated purpose — a different reason is a different *key*, so
`should_log()` already returns `True` on first sighting — and a catastrophe when two
retry loops park one session under different reasons. The stuck-session sweep
(`reassign_failed:stuck_session_sweep`, 300 s) and the rotation monitor
(`reassign_failed:weekly limit reached`, ~60 s) forgot each other's window on every tick,
so every emission reported at WARNING.

**The multiplier.** `_reassign_locked` built the reason by interpolation —
`paused_reason = f"reassign_failed:{reason}"` — putting the *trigger* (which loop asked
for the rebuild) inside the *cause*. One failure therefore had as many reasons as
`_reassign` has callers, which is what made the two loops alternate in the first place.

## Fix shape

1. **Interpolate nothing into a value that becomes a throttle key.** A `LogThrottle` key
   must be bounded in cardinality *by construction* (`server/obs/logdedupe.py`). Move the
   volatile part to the surrounding record, where it is context rather than identity.
2. **Make identity coarse and pacing fine.** Swap `log_throttled` for
   `log_event_throttled`: the fingerprint keys on the event name, the slug rides in
   `fields` where it feeds the issue's title but not its identity, and the throttle key
   stays as specific as the condition.
3. **Delete `forget()` on a value change.** A new value is a new key and reports promptly
   without it; re-arming only ever helps a value the window is *already* holding down,
   which is exactly the flood.
4. **Declare the vocabulary.** A closed `frozenset` of legal values plus an AST guard over
   the call sites turns "don't interpolate this" from a comment into a test.

## Where to look

Any `log_throttled(...)` whose format string interpolates something that is not a uuid, an
int or a quoted string. Sweep with:

```bash
grep -rn "log_throttled(" --include=*.py server/ src/ | grep -v log_event_throttled
```

Known siblings at the time of writing, each still on the plain form:

| Call site | Slug in the message | Issues it can mint |
|---|---|---|
| `server/api/accounts.py:208,212` | account name | one per account |
| `server/sessions/host_probe.py:121,126` | host name, endpoint, `str(exc)` | one per host × per exception text |
| `server/sessions/fleet_orchestrator.py:270` | host, site, cause | one per host |
| `server/sessions/orchestrator.py:19412` | replay surface, scrubbed cause | one per surface × category (#1852 / #1860) |

Prior instances of the same shape, all closed as "one call site, two issue lineages":
#1531 / #1875 and #1533 / #1877 (`src=<slug>` in the feature-doc link warning),
#1874 / #1635 / #1902 / #1903 (the pm-sync siblings), #2101 (the structured-logging fix
that introduced `log_event_throttled`).
