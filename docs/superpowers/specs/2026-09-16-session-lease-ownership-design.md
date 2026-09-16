# Session lease ownership: choke-point fence, build-window grace, self-healing reconcile — Design

Date: 2026-09-16 · Status: draft for review · Refs: #1824 (fixed by #1825), #1826 (open), #1738 (#1763), #1379, #852

## Problem

Session `331f7d98` (row `host=rocky`, real worktree on rocky) was unreachable for a day. The
primary daemon on dobby held its live driver in a bare `mkdir`'d copy of the worktree path,
renewed the session lease, and every attach the edge routed to rocky was refused with
`session.lease_denied`. Three separate gaps combined:

1. **Callers decide ownership, the choke point does not.** `_claim_session` (the one place
   every driver build goes through) only asks the lease "is someone else live?". It never asks
   "does this host own this row?". So every local actor that walks the primary's fleet-wide
   `_records` — autopilot (#1738), the deploy snapshot (#1824), tomorrow's new loop — needs its
   own `_owns_record` check, and one missed caller reopens the bug.
2. **The lease dies during the build.** `_claim_session` records the fence before the driver
   exists; `ensure_driver` then spends 2–20 s spawning the CLI. `_session_lease_tick` (every
   `SESSION_LEASE_TTL_S/5` = 24 s) treats "fence, no driver" as a leak and releases it. A build
   that straddles a tick runs without a lease from then on, and nothing renews it (#1826).
3. **Nothing notices a holder that is not the owner.** Once the wrong daemon holds the lease,
   the state is stable: the holder renews forever, the owner is refused forever, and the
   dashboard shows "idle, detached". The repair was manual.

## Goals

- A daemon can never build a driver for a row another enabled host owns, whoever asks.
- A lease won for a build is held and renewed until the driver exists or the build gives up.
- A lease held for a row this host does not own is detected within one tick and given back.
- No new RPC surface, no schema change, no new environment variables.

## Non-goals

- Rescuing rows stamped for an **enabled but dead** worker. `_owns_record` treats an enabled
  stamp as "that host's responsibility"; rescue is an operator action (disable the host in the
  registry → orphan adoption) or the cross-host transfer initiative (#1696). Unchanged here.
- Dashboard visibility of lease holder vs. routed host, and a one-click reclaim (layer 5).
- Refusing the silent `mkdir` of a foreign cwd (layer 3). With layer 1 the primary never
  reaches that line for a foreign row; the `mkdir` stays for owned rows whose cwd was cleaned.
- Fleet-level integration tests with two daemons on one DB (layer 6) — listed under testing as
  a follow-up, not a deliverable of this spec.

## Key facts (verified 2026-09-16)

- `_claim_session(session_id, *, why)` is called from exactly four sites: spawn
  (`_spawn_session`), resume (`ensure_driver`), adopt (`adopt_session`), relaunch
  (`_reassign_locked`). It sets `_lease_fences[sid]` before any driver is registered.
- The in-flight markers `_spawning` and `_swapping` are both set **after** the claim, so the
  tick cannot use them alone to recognise a build in progress.
- `_session_lease_tick` releases any fence whose sid is not in `_drivers`, renews the rest,
  and logs CRITICAL when a renew is fenced away. `_teardown_driver_state` releases the lease
  synchronously; that is the only other release path besides `stop()`.
- `_owns_record(record, enabled_hosts)`: live driver → owned; worker → `host == me`; primary →
  owned when host is NULL / me / `DEFAULT_HOST_NAME`, or when the stamp has left the registry;
  `enabled_hosts is None` (registry unreadable) → foreign stamps are **not** owned (fail closed).
- `session_lease_enforced()` (`CCSWITCH_SESSION_LEASE_ENFORCE`, default on) already turns the
  lease refusal into a warning-and-proceed. `SPAWN_WAIT_S` (default 60 s) is the existing
  ceiling on how long an attach waits for an in-flight build.
- `primary_lease.release` marks `renewed_at = 1970-01-01`; `acquire` wins on own holder or a
  lapsed heartbeat and bumps the fence. The module logs every lease name as `primary-lease:`.

## Decisions

| # | Decision | Default chosen | Why |
|---|----------|----------------|-----|
| D1 | Where the ownership fence lives | Inside `_claim_session`, before `acquire` | One choke point; callers cannot forget it. |
| D2 | How explicit adoption bypasses it | Keyword `adopt: bool = False`; only `adopt_session` (and a future `transfer_import`) pass `True` | Adoption paths deliberately take a row they do not yet own. |
| D3 | Row with no in-memory record | Not fenced (lease alone decides) | A fresh spawn has no record yet; rehydrate has already host-filtered workers. |
| D4 | Kill switch | Reuse `CCSWITCH_SESSION_LEASE_ENFORCE` for all three layers | It already means "the lease guard is advisory"; no new setting, no ratchet hit. |
| D5 | Build-window grace | `SPAWN_WAIT_S`, measured from the claim | Same ceiling attaches already wait; a build slower than that is treated as failed today. |
| D6 | Reconcile action when holder ≠ owner | Tear down our driver when no turn is active; otherwise log CRITICAL and retry next tick | Killing a driver mid-turn has its own failure modes (existing comment in the tick). |
| D7 | Reconcile when the registry is unknown | Skip (log only) | A registry blip must never make a daemon drop its own live drivers. |

## Design

### Layer 1 — ownership fence at the choke point

`_claim_session(session_id, *, why, adopt=False)`:

```
rec = self._records.get(session_id)
if rec is not None and not adopt:
    enabled = await self._enabled_host_names()
    if not self._owns_record(rec, enabled):
        if session_lease_enforced():
            log_event(WARNING, "session.lease_denied", session_id, holder=rec.host,
                      reason=f"foreign_stamp:{why}")
            return False
        log.warning("... stamped for %r but enforcement is OFF — claiming anyway (%s)")
# existing acquire / refuse / fail-open logic unchanged
```

- The check runs **before** `acquire`, so a refused claim never touches the lease row and never
  bumps the fence.
- `adopt_session` passes `adopt=True`. `_reassign_locked` passes nothing: its record normally
  still has the live driver (owned by the first clause of `_owns_record`); if the row is
  foreign-stamped and the driver is already gone, refusing the relaunch is the correct outcome
  and the caller's existing `except SessionLeaseHeld` pauses the session for a human.
- `_spawn_session` passes nothing: the record is not in `_records` yet at claim time (D3), and
  the row it creates is stamped with `self_host_name()`.
- On a worker, `_owns_record` is `host == me`, so a worker refuses to build for a primary-stamped
  or NULL-stamped row even if an RPC asks it to. Today rehydrate hides those rows; this makes the
  refusal explicit.
- The per-caller fences added by #1763 and #1825 stay. They are cheaper (no lease round-trip)
  and give better log lines; layer 1 is the backstop for callers that do not have one.

### Layer 2 — the lease survives the build window

Add `self._lease_building: dict[str, float]` (sid → `time.monotonic()` of the claim).

- `_claim_session` sets `_lease_building[sid] = now` on every successful claim (fresh or
  re-acquired). `_release_session_lease` pops it.
- `_session_lease_tick` becomes:

```
for sid, fence in fences:
    if sid in self._drivers:
        self._lease_building.pop(sid, None)
        renew(sid, fence)                      # unchanged
    elif now - self._lease_building.get(sid, -inf) < SPAWN_WAIT_S:
        renew(sid, fence)                      # building: keep the heartbeat fresh
    else:
        release(sid)                           # leaked or failed build: unchanged
```

- Renewing (not just skipping) during the build keeps `renewed_at` fresh, so a build longer than
  `SESSION_LEASE_TTL_S` (120 s) does not look lapsed to another host.
- No registration site changes: the first tick that sees the driver clears the marker. A build
  that fails after the claim keeps the lease for at most `SPAWN_WAIT_S`, then the existing
  release rule reclaims it. A failed `renew` during the build follows the existing fenced-away
  path (pop the fence, log CRITICAL).

### Layer 4 — self-healing reconcile

In the same tick, for every fence whose sid **is** in `_drivers`:

```
rec = self._records.get(sid)
if rec is None or enabled is None:            # D7: unknown registry → never destructive
    continue
if self._owns_record_by_stamp(rec, enabled):  # _owns_record minus the live-driver clause
    continue
driver = self._drivers[sid]
if driver.turn_active or driver.drain_alive:
    log_event(CRITICAL, "session.lease_foreign_holder", session_id, host=rec.host,
              action="deferred_turn_active")
    continue
if not session_lease_enforced():
    log_event(CRITICAL, ..., action="log_only")
    continue
driver = await self._teardown_driver_state(sid)   # pops driver, releases lease
self._spawn(self._close_automation_driver(driver, sid), name=f"lease-reconcile-{sid}")
log_event(WARNING, "session.lease_reconciled", session_id, host=rec.host)
```

- `_owns_record` cannot be used as-is because its first clause says "live driver here → owned",
  which is exactly the state being questioned. Factor the stamp-only rule out of `_owns_record`
  into `_owns_record_by_stamp` and have `_owns_record` call it after the driver clause, so there
  is still one source of truth for the stamp policy.
- The record stays in `_records` (it is a fleet mirror on the primary); `_sync_detached` derives
  `detached=True` on the next read. The owner's next attach or restart resumes it normally,
  because `_teardown_driver_state` released the lease synchronously.
- `enabled` is read once per tick via the cached `_enabled_host_names()`; the tick already runs
  every 24 s, so this adds at most one cached lookup per tick.
- This is the automated form of the manual repair performed on 2026-09-16 (archive on the
  holder, resume on the owner), minus the archive: the driver is closed, the row is untouched.

### Ordering inside the tick

1. Read `enabled = await self._enabled_host_names()` once.
2. For each fence: live driver → layer 4 check, then renew; building → renew; else release.

Layer 4 runs before the renew so a reconciled session is released, not renewed, in the same pass.

### Observability

All new signals are `log_event` entries the journal already captures; no new API fields.

| Event | Level | When |
|-------|-------|------|
| `session.lease_denied` `reason=foreign_stamp:<why>` | WARNING | Layer 1 refusal (reuses the existing event name; the reason prefix distinguishes it from a live-twin refusal) |
| `session.lease_foreign_holder` `action=deferred_turn_active\|log_only` | CRITICAL | Layer 4 found a foreign-owned live driver and did not act |
| `session.lease_reconciled` | WARNING | Layer 4 tore the driver down and released |

## Data-flow walkthroughs

- **The incident, replayed.** Pre-#1763 autopilot calls `ensure_driver` on the rocky row from
  dobby → layer 1 refuses before `acquire`; no lease, no `mkdir`, no driver. Even if it had
  slipped through, the next tick's layer 4 closes the driver and releases; rocky's next attach
  resumes it. The deploy snapshot never contains the row because no driver was ever built.
- **Normal resume.** Rocky claims at t=0, builds for 18 s. Tick at t=1 sees no driver but a
  1-second-old build marker → renews. Tick at t=25 sees the driver → clears the marker, renews.
  Lease renewed throughout; #1826 closed.
- **Failed build.** Claim at t=0, spawn raises at t=3, fence remains. Ticks renew until t=60,
  then release. A twin on another host is blocked for at most 60 s longer than today.
- **Registry blip.** `_enabled_host_names()` returns `None`. Layer 1 refuses foreign stamps
  (fail closed, as #1763 chose). Layer 4 skips entirely (D7). Own rows unaffected.
- **Adopt / transfer.** `adopt_session` passes `adopt=True`; the row is new and unstamped, so
  the fence would pass anyway, but the flag documents intent and is what `transfer_import`
  (#1696) will use when it takes a row stamped for the source host.
- **Enabled worker goes down.** Its rows stay stamped for it; the primary refuses to claim them
  (non-goal). Operator disables the host → the stamp leaves the registry → orphan adoption.

## Testing (TDD)

Existing harnesses: `test_session_lease.py` (real orchestrator + SQLite lease rows,
`_age_lease`), `test_recovery_actor_ownership.py` (`__new__`-built actor with `_owns_record`
and a mocked `_enabled_host_names`).

Layer 1
- Primary, record stamped `rocky`, registry `{dobby, rocky}`: `_claim_session(why="resume")`
  returns False, no `daemon_leases` row is written, `session.lease_denied` carries
  `reason=foreign_stamp:resume`.
- Same with registry `None`: refused.
- Same with `adopt=True`: claims.
- Stamp `gone` (left the registry) and stamp `None`: claims.
- Worker `rocky`, record stamped `dobby`: refused; stamped `rocky`: claims.
- Enforcement off: claims and logs the warning.
- `ensure_driver` end-to-end on a foreign-stamped record returns `None` and never calls the SDK
  factory (pin that the `mkdir` never happens: `cwd` does not exist afterwards).
- `adopt_session` still builds (existing adopt tests stay green).

Layer 2
- Claim, run one tick with no driver: lease still held, `renewed_at` advanced, fence intact.
- Register the driver, tick: marker cleared, renewed.
- Claim, advance the monotonic clock past `SPAWN_WAIT_S`, tick with no driver: released.
- Renew fenced away during the build: fence popped, CRITICAL logged (existing behaviour).

Layer 4
- Live driver for a foreign-stamped record, no turn active: after one tick the driver is gone
  from `_drivers`, the lease row is released (`renewed_at` epoch), `session.lease_reconciled`
  logged; the record remains and reads `detached=True`.
- Same with `turn_active=True`: driver kept, CRITICAL with `action=deferred_turn_active`.
- Same with registry `None`: nothing happens.
- Same with enforcement off: driver kept, CRITICAL with `action=log_only`.
- Own-stamped, NULL-stamped and orphaned rows are never reconciled.
- `_owns_record` behaviour table from `test_recovery_actor_ownership.py` still holds after the
  `_owns_record_by_stamp` split.

Follow-up (not in this spec): a two-orchestrator test on one SQLite file that snapshots a
foreign row on the "primary", restarts it, and asserts the "worker" resumes — the fleet-shaped
regression that would have caught #1824 and #1826 together.

## Rollout and risks

- One PR; all three layers are small and share the tick. Land behind the existing
  `CCSWITCH_SESSION_LEASE_ENFORCE` switch so a bad interaction can be made advisory without a
  rollback.
- **Risk: layer 4 closes a driver that was legitimately adopted in place.** Adopted rows are
  stamped for the adopting host by `adopt_session`, so they are owned. Verify with the adopt
  tests before merging.
- **Risk: a re-stamp while a driver is live** (future transfer) would trigger layer 4 on the
  source host. That is the desired handover behaviour and matches #1696's design, which
  expects the source to drop its driver after export.
- **Risk: the tick now does a cached registry read.** `_enabled_host_names` is TTL-cached; the
  cost is one DB read per cache expiry, not per tick.
- After deploy, confirm on both hosts that `session.lease_denied reason=foreign_stamp` never
  fires for own rows during a normal day, and that every session lease row for a live driver
  shows a fresh `renewed_at`.

## Open decisions for review

Defaults are chosen above; flag any you want changed before planning.

1. **D6 — teardown vs. log-only for layer 4.** Teardown is the recommended default because a
   log nobody reads is how this incident lasted a day. Log-only would still need the manual
   repair.
2. **D4 — one kill switch for all layers.** Simpler and no new setting; the alternative is a
   `VIBERPIT_LEASE_OWNERSHIP_ENFORCE` that lets layer 4 be disabled while the lease refusal
   stays on.
3. **Non-goal on dead workers.** Keeping rows pinned to a dead enabled worker is the existing
   policy; if you would rather the primary rescue them after the host heartbeat lapses, that is
   a separate decision with its own spec.
