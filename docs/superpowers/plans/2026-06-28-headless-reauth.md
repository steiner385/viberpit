# Headless Account Reauth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-authenticate accounts headlessly and remotely, with the rare residual handled by a host-resident passkey + transient 1Password password fetch — and first fix the cheap in-session 401 defect that causes 100% of current `needs-reauth` flips.

**Architecture:** Three phases. **Phase 1** (executable now) hardens the in-session 401 recovery path that the diagnosis pinned as the sole current cause — force a token refresh before recompose, and record token expiry in the flip audit so we can prove expired-vs-revoked. **Phase 2** (executable now) runs three verification spikes that gate the passkey pipeline. **Phase 3** (GATED on Phase 2) builds the host-resident-passkey re-grant pipeline; its bite-sized TDD steps are finalized only after the spikes prove the integration, because the steps depend on unproven CDP/Google behavior and writing fictional test code now would violate the no-placeholders rule and constitution Principle III (verify against the real binary/API).

**Tech Stack:** Python (FastAPI/asyncio backend, `server/`), `ccswitch` core (`src/ccswitch/`), pytest. Phase 3 adds Chrome DevTools Protocol (virtual WebAuthn authenticator), the `op` 1Password CLI (2.34.0, present on host), and `secret-tool`/GNOME keyring (already the project's secret backend).

**Spec:** `docs/superpowers/specs/2026-06-28-headless-reauth-design.md`

## Global Constraints

- Never fork/patch the `claude` binary — the binary owns the OAuth dance (constitution I).
- Account isolation: per-process `CLAUDE_CONFIG_DIR`, strict per-account profiles (constitution II).
- Credential material is NEVER logged or printed (extends `capture_login`'s existing discipline + audit redaction layer).
- New model columns must be nullable (`init_db` backfills nullable; NOT NULL needs a real migration).
- Backend lint gates run first in CI: `ruff` + `mypy` + `pytest server/tests/`. Run all three locally.
- Worktree backend tests: run against the main checkout's prebuilt venv with `PYTHONPATH=<worktree>/src`; full-suite has ~160 environmental failures — judge by diffing failure-node-id sets, not raw counts; per-file runs are authoritative for new tests.
- Secrets route through the EXISTING keyring/crypto integration (`src/ccswitch/crypto.py` `secret-tool` + envelope encryption) — no parallel secrets subsystem (eliminating-parallel-implementations).
- Completion workflow: branch off `origin/master` (never local `master`), one PR per phase, mark ready + auto-merge.

---

## PHASE 1 — In-session 401 hardening (executable now, standalone PR)

This phase is independently shippable and addresses the entire current failure population
(27/27 flips are `path:"live-session"` 401s). It does not touch the passkey work.

### Task 1: Force a token refresh before recompose on the first in-session 401

The first-401 recovery (`_recompose_same_account` → `_reassign` → `compose`) re-assembles
stored creds and relaunches, but never refreshes the token. If the stored access token is
stale, the relaunch presents the same stale token, a second 401 lands within 60s, and the
account is falsely flipped to `needs-reauth`. Forcing `ensure_valid_token` (which refreshes +
persists to the slot that `compose` reads) before relaunch fixes the dominant case.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_recompose_same_account`, ~line 2338)
- Test: `server/tests/test_orchestrator_recovery.py`

**Interfaces:**
- Consumes: `ccswitch_accounts.ensure_valid_token(name: str) -> dict` (already imported as
  `ccswitch_accounts` at `orchestrator.py:30`; refreshes + persists to `paths.slot_path(name)`).
- Produces: no signature change to `_recompose_same_account(self, record) -> None`; adds a
  best-effort refresh side effect before `_reassign`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_orchestrator_recovery.py`. This test uses the REAL
`_recompose_same_account` (not the spy in `_make_orch`), so build the orchestrator directly and
spy only `_reassign`.

```python
def test_recompose_forces_token_refresh_before_reassign(monkeypatch):
    from unittest import mock
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    order = []

    async def spy_reassign(record, new_account, *, reason):
        order.append(("reassign", new_account, reason))
    o._reassign = spy_reassign

    rec = SessionRecord(id="s1", name="n", account="kindash", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x")
    o._records["s1"] = rec
    o._drivers["s1"] = object()

    ca = mock.MagicMock()
    ca.ensure_valid_token.side_effect = lambda name: order.append(("refresh", name))
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)

    asyncio.run(o._recompose_same_account(rec))

    assert order == [("refresh", "kindash"), ("reassign", "kindash", "recompose")], order
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py::test_recompose_forces_token_refresh_before_reassign -v`
Expected: FAIL — `order == [("reassign", ...)]` only (no `("refresh", ...)` precedes it).

- [ ] **Step 3: Write minimal implementation**

Replace `_recompose_same_account` in `server/sessions/orchestrator.py`:

```python
    async def _recompose_same_account(self, record: SessionRecord) -> None:
        """Heal a clobbered live .credentials.json. Force a token refresh FIRST so the
        relaunch picks up a fresh access token (the dominant in-session 401 cause — a stale
        stored token would otherwise 401 again immediately and falsely flip needs-reauth),
        then recompose from the store and relaunch the same account in place."""
        try:
            await asyncio.to_thread(ccswitch_accounts.ensure_valid_token, record.account)
        except Exception:
            log.warning("recompose.ensure_valid_token failed account=%s",
                        record.account, exc_info=True)
        await self._reassign(record, record.account, reason="recompose")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py::test_recompose_forces_token_refresh_before_reassign -v`
Expected: PASS

- [ ] **Step 5: Run the existing recovery suite to confirm no regression**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_recovery.py -v`
Expected: all PASS (the spy-based `_make_orch` tests are unaffected — they replace
`_recompose_same_account` wholesale).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_recovery.py
git commit -m "fix(reauth): force token refresh before in-session 401 recompose

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 2: Record token expiry in the live-session needs-reauth audit

The flip site hardcodes `token_expires_at=None`, so audit rows can't distinguish an expired
token (refreshable) from a still-valid-but-revoked grant. Populate it from the existing
`_slot_expires_at` helper to close the diagnostic blind spot called out in the spec.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_mark_needs_reauth_audit`, ~line 2389)
- Test: `server/tests/test_orchestrator_token_refresh.py`

**Interfaces:**
- Consumes: `ccswitch_accounts._slot_expires_at(name: str) -> int | None` (`accounts.py:110`;
  reads `expiresAt` ms-epoch from the slot, returns None on any error).
- Produces: the `account.needs_reauth` audit `detail["token_expires_at"]` is the slot's
  `expiresAt` instead of always None.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_orchestrator_token_refresh.py`:

```python
@pytest.mark.asyncio
async def test_mark_needs_reauth_audit_records_token_expiry(monkeypatch):
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import AuditEvent
    from server.sessions import orchestrator as orch_mod
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    orch = _orch()
    ca = mock.MagicMock()
    ca._slot_expires_at.return_value = 1893456000000  # fixed ms-epoch
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)
    await orch._mark_needs_reauth_audit("primary")
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent).where(
            AuditEvent.type == "account.needs_reauth"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["token_expires_at"] == 1893456000000
    ca._slot_expires_at.assert_called_once_with("primary")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py::test_mark_needs_reauth_audit_records_token_expiry -v`
Expected: FAIL — `detail["token_expires_at"]` is None.

- [ ] **Step 3: Write minimal implementation**

In `_mark_needs_reauth_audit`, change the `AuthFailure` construction from
`token_expires_at=None` to read the slot:

```python
        f = AuthFailure(account=account, status=401, body=None,
                        path="live-session",
                        token_expires_at=ccswitch_accounts._slot_expires_at(account),
                        attempts=1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py::test_mark_needs_reauth_audit_records_token_expiry -v`
Expected: PASS

- [ ] **Step 5: Confirm the existing audit test still passes**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_orchestrator_token_refresh.py -v`
Expected: all PASS (`test_mark_needs_reauth_writes_audit_event` asserts `path` + `account`
only; the now-populated expiry doesn't break it).

- [ ] **Step 6: Lint + commit**

```bash
<main-checkout>/.venv/bin/ruff check server/sessions/orchestrator.py
<main-checkout>/.venv/bin/mypy server/sessions/orchestrator.py
git add server/sessions/orchestrator.py server/tests/test_orchestrator_token_refresh.py
git commit -m "feat(reauth): record token expiry in live-session needs-reauth audit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

### Task 3: Open the Phase 1 PR

- [ ] **Step 1: Branch, push, PR** (the two commits should already be on a feature branch
  per the completion workflow; if committed on the worktree branch, create the feature branch
  off `origin/master` first and cherry-pick).

```bash
git fetch origin
git checkout -b fix/in-session-401-force-refresh origin/master
git cherry-pick <task1-sha> <task2-sha>
git push -u origin fix/in-session-401-force-refresh
gh pr create --title "fix(reauth): harden in-session 401 recovery (force refresh + audit expiry)" \
  --body "$(cat <<'EOF'
Addresses the diagnosed cause of 100% of current needs-reauth flips (27/27 are
live-session 401s, zero refresh-endpoint). Forces ensure_valid_token before the first-401
recompose so a merely-stale token is refreshed instead of falsely flipping the account, and
records token_expires_at in the flip audit to distinguish expired-vs-revoked going forward.

Spec: docs/superpowers/specs/2026-06-28-headless-reauth-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge <#> --squash --auto
```

- [ ] **Step 2: Observe production audit rows for ~3 days.** Re-run the diagnosis query
  (`SELECT json_extract(detail,'$.token_expires_at') ... FROM audit_events WHERE
  type='account.needs_reauth'`) to measure how many remaining flips are expired (now fixed)
  vs still-valid-grant-revoked (need Phase 3). This number sizes Phase 3's urgency.

---

## PHASE 2 — Verification spikes (executable now; gate Phase 3)

These are research/proof tasks, not TDD. Each produces a written finding committed to
`docs/superpowers/specs/spikes/`. Phase 3's design is conditional on their outcomes.

### Spike A: CDP virtual authenticator attaches to the login browser

- [ ] Launch a Chromium via the `BROWSER` launcher env path used by the reauth runner
  (`server/scripts/ccswitch-reauth-browser`) with `--remote-debugging-port`.
- [ ] Via CDP, call `WebAuthn.enable`, `WebAuthn.addVirtualAuthenticator`
  (`protocol:"ctap2"`, `transport:"internal"`, `hasResidentKey:true`, `hasUserVerification:true`,
  `isUserVerified:true`, `automaticPresenceSimulation:true`), and `WebAuthn.addCredential`.
- [ ] Drive a WebAuthn test page (e.g. webauthn.io) and confirm a registration + assertion
  complete with no human gesture.
- [ ] **Decision criterion:** if attach + assertion succeed → Phase 3 primary path is viable.
  If the `claude auth login` launcher cannot be made to expose a debugging port → fall back to
  launching our own Chromium and handing the binary the resulting profile.
- [ ] Commit finding: `docs/superpowers/specs/spikes/2026-spike-cdp-virtual-authenticator.md`.

### Spike B: Google accepts a virtual-authenticator passkey from the server IP

- [ ] On a THROWAWAY Google test account, register a passkey via the host's virtual
  authenticator, then sign out and re-authenticate using only that passkey, from the server.
- [ ] Repeat across several hours/days to observe risk-based escalation frequency.
- [ ] **Decision criterion:** measure the % of re-auths that complete passkey-only vs escalate
  to password/other. This sizes how often Phase 3's password rung fires. If escalation is
  frequent → reconsider password-primary, or accept higher `op` reliance.
- [ ] Commit finding: `docs/superpowers/specs/spikes/2026-spike-google-server-ip-passkey.md`.

### Spike C: Keyring unlock after host reboot

- [ ] Reboot the host (or simulate a fresh login session) and check whether
  `secret-tool lookup service ccswitch username master-key` succeeds without manual unlock.
- [ ] Evaluate unlock strategies: PAM auto-unlock at the service user's login,
  `CCSWITCH_MASTER_KEY` env injection at daemon start, or an explicit unlock step.
- [ ] **Decision criterion:** choose the strategy that keeps the `op` token + master key
  readable by the daemon across reboots without storing the unlock secret in plaintext.
- [ ] Commit finding: `docs/superpowers/specs/spikes/2026-spike-keyring-reboot.md`.

---

## PHASE 3 — Host-resident passkey re-grant pipeline (GATED on Phase 2)

> Do NOT begin Phase 3 until Spikes A–C are resolved. The bite-sized TDD steps for these tasks
> will be written into this section after the spikes confirm the integration shapes (CDP attach
> method, escalation frequency, keyring unlock). Writing them now would require fabricating test
> expectations for unproven external behavior.

### File structure (locked in now)

- Create: `server/reauth/profile_store.py` — `ProfileStore`: persistent per-account Chrome
  profile under `paths.reauth_profiles_dir()/<account>`; `checkout(account) -> str` (path),
  `strip_anthropic_cookies(path)`, warmth check.
- Create: `server/reauth/passkey_vault.py` — `PasskeyVault`: per-account `google.com`-scoped
  credential, `get(account) -> PasskeyCredential | None`, `store(account, cred)`; private key
  encrypted via `ccswitch.crypto.encrypt`; CDP injection helpers.
- Create: `server/reauth/op_fallback.py` — `fetch_google_password(account) -> str`: reads the
  `op` service-account token from the keyring (`secret-tool`), runs
  `op read "op://<vault>/<account>/password"`, returns transiently; never persists/logs.
- Create: `server/reauth/grant_runner.py` — `GrantRunner.run(account) -> GrantOutcome`
  (`granted | needs_password | needs_human | failed`); wraps `capture_login` with the
  ProfileStore profile + virtual display + CDP passkey injection + `LoginObserver`.
- Create: `server/reauth/login_observer.py` — `LoginObserver`: passive CDP page-state
  classifier → terminal `GrantOutcome`.
- Create: `server/reauth/coordinator.py` — `ReauthCoordinator`: single-consumer asyncio.Queue,
  per-account dedup set, cooldown/backoff; `enqueue(account, reason)`; drains run-to-completion.
- Modify: `src/ccswitch/paths.py` — add `reauth_profiles_dir()`.
- Modify: `src/ccswitch/crypto.py` — add public `store_secret_in_keyring(name, value)` /
  `read_secret_from_keyring(name)` wrappers around the existing `secret-tool` pattern (for the
  `op` token), if Spike C's strategy is keyring-based.
- Modify: `server/sessions/orchestrator.py` — wire `ReauthCoordinator`; proactive heartbeat in
  `_refresh_idle_tokens_once`; reactive `enqueue` in the `auth_401` escalation branch (replacing
  the immediate `_mark_needs_reauth`, which becomes the terminal rung).
- Modify: `server/api/accounts.py` — the add/reauth entry point must `cancel()` an in-flight
  BACKGROUND coordinator run before `start()`, so a manual login is never blocked by a
  background warm-up (Concurrency Caveat 1 in the spec).

### Task list (to be expanded to bite-sized TDD post-spike)

1. `paths.reauth_profiles_dir()` + `ProfileStore` (persistent profile create/checkout/strip).
2. `PasskeyVault` store/get with envelope encryption (no CDP yet — pure crypto + storage).
3. CDP passkey injection (shape from Spike A) — attach + inject credential.
4. `LoginObserver` page-state classification → `GrantOutcome`.
5. `op_fallback.fetch_google_password` (token from keyring → `op read`; never logged).
6. `GrantRunner.run` orchestrating profile + display + passkey + observer + password rung.
7. `ReauthCoordinator` queue: dedup, serial drain, run-to-completion, cooldown/backoff.
8. Orchestrator wiring: proactive heartbeat enqueue + reactive escalation enqueue + demote
   `_mark_needs_reauth` to terminal.
9. User-preemption: add/reauth cancels in-flight background run, coordinator re-enqueues.
10. Per-account bootstrap command/runbook: register the host passkey on each Google account +
    create the 1Password vault items + store the `op` service-account token (one-time, interactive).

### Concurrency validation to encode as tests (Phase 3)

- Three accounts enqueued → drain serially, dedup prevents double-enqueue, no two runs overlap.
- A manual add/reauth during a background run → background run cancelled + re-enqueued, manual
  login proceeds without `ReauthBusy`.
- An escalation run exceeding the browser-occupancy timeout → cancelled, parked, next item runs.

---

## Self-Review

**Spec coverage:**
- Diagnosis fix (force-refresh first rung) → Phase 1 Task 1. ✓
- Diagnostic blind spot (record token expiry) → Phase 1 Task 2. ✓
- Host-resident passkey primary path → Phase 3 files + tasks 1–4, 6. ✓
- Transient 1Password password fallback → Phase 3 task 5. ✓
- Secrets model via existing keyring/crypto → Global Constraints + Phase 3 crypto wrappers. ✓
- ProfileStore replaces single shared profile → Phase 3 file structure. ✓
- ReauthCoordinator serial queue + three-account validation → Phase 3 task 7 + concurrency tests. ✓
- User-preemption (Caveat 1) → Phase 3 task 9. ✓
- Head-of-line/browser-occupancy timeout (Caveat 2) → Phase 3 concurrency tests. ✓
- Three verification spikes → Phase 2. ✓
- Keyring-after-reboot failure mode → Spike C + Phase 3 crypto wrapper. ✓

**Placeholder scan:** Phase 1 + Phase 2 contain complete code/commands. Phase 3 is
deliberately a skeleton (file structure + interfaces + task list) GATED on spikes; this is an
explicit decomposition decision, not a hidden placeholder — its bite-sized steps are written
after Phase 2 resolves the unproven integration shapes.

**Type consistency:** `ensure_valid_token(name) -> dict`, `_slot_expires_at(name) -> int|None`,
`GrantOutcome` enum, `ReauthCoordinator.enqueue(account, reason)` used consistently across
phases.

**Recommendation:** treat Phase 1 as its own PR/plan (ship now — it likely resolves most flips),
Phase 2 as a spike PR, and re-enter writing-plans to expand Phase 3 once spikes land.
