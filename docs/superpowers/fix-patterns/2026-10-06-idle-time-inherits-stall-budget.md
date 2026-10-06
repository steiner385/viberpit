# Idle time inherited by a new turn (#2724)

**Symptom:** A prompt appears in the transcript, immediately ends as interrupted,
and leaves the session waiting for the user without an answer.

**Anti-pattern:** A long-lived driver enters a new active turn while retaining the
previous turn's progress timestamp. The stall watchdog counts idle time as a hang
in the newly submitted work.

**Fix shape:** Reset the progress baseline before publishing the active state or
awaiting the start request. Do this for native/autonomous starts too, but do not
let duplicate start notifications or empty deltas renew an active turn's budget.
Test both protection of a fresh turn after idle and interruption after a genuine
full silence window.

**Where to look:** Codex `_start_turn` and inactive-to-active `TurnStart`
consumption; Claude `_begin_turn` and `_open_autonomous_turn`; the shared
`SessionOrchestrator._check_stalls` consumer. The audit found and fixed all four
driver entry paths. Queued prompts use the same start paths; steering an already
active turn intentionally does not reset its progress clock.
