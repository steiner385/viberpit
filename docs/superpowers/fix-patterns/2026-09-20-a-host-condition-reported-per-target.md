# A host condition reported per target

## Symptom

Six `[auto-error]` issues, one fact. On 2026-09-17 dobby lost its route to
api.github.com for roughly six minutes and ViberPit filed five issues for it:

| issue | logger | what actually varied |
|---|---|---|
| #1922 | `server.github.ci_issues` | `repo=steiner385/viberpit` |
| #1923 | `server.github.deployments` | a **four**-repo list |
| #1924 | `server.github.deployments` | a **five**-repo list |
| #1926 | `server.github.pr_poller` | `owner=cairnea` |
| #1927 | `server.github.pr_poller` | `owner=steiner385` |

#1941 is the same story for a second condition (gh rate-limited with no
credential). Every one of these repos and owners was healthy. None of the six
issues named the thing that was broken.

## Anti-pattern

**Reporting the target you failed to reach instead of the condition that stopped
you.** It is the natural thing to write — `log.warning("…_failed repo=%s: %s",
repo, proc.stderr)` — and it is right when the *target* is the problem. It is
exactly wrong when the host is, and the host is the problem more often, because
a locked keyring, a dead network or an exhausted budget breaks every loop at once.

Three splitters, each of which alone is enough to fork the lineage, because
`signature_of` keys on `(source, level, logger, event-or-normalized-message,
exc_class, frame)`:

1. **A bare slug in the message.** `owner=cairnea` vs `owner=steiner385`.
   `normalize()` masks uuids, quoted runs, hex and numbers — a slug is none of
   those.
2. **Arity, even when content is already masked.** #1923/#1924 is the instructive
   pair: the repos were passed as a *list*, so their names were already quoted
   and collapsed to `<str>` — and the issues still split, because
   `repos=[<str>, <str>, <str>, <str>]` is not the five-element string. Quoting
   the volatile value does not help when its *length* is volatile too.
3. **The logger.** This is the one that survives a partial fix. #2117 had
   already collapsed ci_issues' four gh paths onto one throttled event, and
   #1922 still filed separately from #1926, because the helper logged on the
   *caller's* logger and the logger is part of the key.

## Fix shape

**Name the condition in the core, collapse the record at one shared point.**

- `ccswitch/gh_credential.py:classify_gh_failure` is the single place that reads
  gh stderr and names a host condition. It returns a closed vocabulary
  (`CONDITIONS`), so a consumer can assert it handles all of them rather than
  meeting a new one in production.
- `server/github/host_failures.py:log_gh_failure` is the single place that
  decides what to log. For a host condition it demotes the per-target record to
  DEBUG (never drops it — whoever turned on debug logging to watch the outage
  still needs to know which calls were lost) and emits one throttled structured
  event. For anything else it emits the call site's record verbatim.

Three things have to be constant at once for one condition to be one issue, and
the shared module is what supplies all three:

- **one logger** — `logging.getLogger("server.github.host")`, deliberately *not*
  `__name__` and deliberately not the caller's;
- **one frame** — `emit_event` is where `logger.log` is actually called, so
  `module:funcName` is constant by construction;
- **one message** — a structured `event=` from the closed vocabulary, with the
  volatile parts in `fields`, which feed the issue title but not its identity.

Split no further and no less: unlocking a keyring, waiting out a rate limit and
fixing a network are three fixes, so they are three event names — the
mirror-image defect is [the condition belongs in the event
name](2026-09-20-the-condition-belongs-in-the-event-name.md).

`exc_info` rides the per-target record only. The last two fingerprint components
are the exception class and the innermost app frame, so a traceback on the
collapsed event would re-split one offline host by subprocess failure mode.

## Not the same as: throttling

Throttling is the other half and does not substitute for this one.
`LogThrottle` lets exactly one record escape per window, so within a single
outage a per-caller logger *looks* collapsed — it just files under whichever
loop lost the race. Across restarts that is a different loop each time, and the
fork is silent.

That is a trap for the test, not just the fix: a guard that drives every loop in
one window passes with or without the shared logger. The assertion has to drive
each loop as **the first to notice in its own window**
(`test_whichever_loop_notices_first_files_the_same_issue`), which is the only
way the decision is observable. This was caught by mutating the fix, not by
reading it.

## How to check for siblings

Any WARNING that reports a subprocess/RPC failure by naming its target:

```bash
grep -rn "log\.\(warning\|error\)" server/ --include=*.py -A2 | grep -i stderr
```

The 2026-09-20 sweep found eleven in `server/github/`, `server/automations/` and
`server/sessions/`, of which the six issues above had exercised three. Assert on
fingerprints via `signature_of`, never on a count of log lines — counting passes
on a change that collapses the records while still minting a fingerprint per
logger, which is precisely how the partial fix looked correct.

Related: [a slug in a throttled record splits one
condition](2026-09-20-a-slug-in-a-throttled-record-splits-one-condition.md).
