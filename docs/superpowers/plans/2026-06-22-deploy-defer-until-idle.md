# Deploy Defer-Until-Idle (Phase B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the auto-deployer from restarting `ccswitch.service` while a session is mid-turn, so deploys land only at an idle point where the existing resume+replay machinery is lossless.

**Architecture:** Three small, independent pieces, no process split: (1) a read-only `restart_safe()` on the orchestrator derived from existing in-memory `_records`; (2) `/healthz` exposes `busy`/`activeSessions`; (3) `scripts/auto-deploy.sh` adds an idle gate before the pull, with a SHA-keyed defer marker and a max-defer cap.

**Tech Stack:** Python 3.12 / FastAPI (backend), bash + bats (deploy script + tests), pytest.

## Global Constraints

- Run backend tests with the main checkout's prebuilt env: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <path> -q -p no:cacheprovider`.
- Active (restart-blocking) statuses are exactly `{"in_progress", "working", "waiting_input"}`. Safe statuses: `idle, ready_review, done, limited, ended, archived`.
- `/healthz` must stay unauthenticated (deploy probes it without an identity header) and must keep returning `{"status":"ok","sha":…}` plus the new fields; the schema-error branch keeps `status:schema_error` + 500.
- Deploy script test seams mirror the existing ones: `CCSWITCH_DEPLOY_CI_OK`, `CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC`. New: `CCSWITCH_DEPLOY_BUSY` (`1`/`0`) and `CCSWITCH_DEPLOY_DEFER_FILE` (override marker path).
- Max-defer env: `CCSWITCH_DEPLOY_MAX_DEFER_SEC`, default `1800`.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Orchestrator `restart_safe()` / `active_session_count()`

**Files:**
- Modify: `server/sessions/orchestrator.py` (add a module-level constant near the top of the file, after imports; add two methods to `SessionOrchestrator`)
- Test: `server/tests/test_orchestrator_restart_safe.py` (create)

**Interfaces:**
- Consumes: `SessionOrchestrator(sdk_factory=...)`, `server.sessions.types.SessionRecord`, `server.tests.fakes.fake_factory.fake_sdk_factory`.
- Produces:
  - `SessionOrchestrator.active_session_count() -> int`
  - `SessionOrchestrator.restart_safe() -> bool`
  - module constant `server.sessions.orchestrator._ACTIVE_STATUSES: frozenset[str]`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_orchestrator_restart_safe.py`:

```python
"""restart_safe(): True only when no session is mid-turn or awaiting input."""
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _rec(sid: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=sid, name=sid, account="acct", claude_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status=status,
    )


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def test_no_sessions_is_safe():
    o = _orch()
    assert o.active_session_count() == 0
    assert o.restart_safe() is True


def test_idle_and_terminal_statuses_are_safe():
    o = _orch()
    for i, s in enumerate(["idle", "ready_review", "done", "limited", "ended", "archived"]):
        o._records[f"s{i}"] = _rec(f"s{i}", s)
    assert o.active_session_count() == 0
    assert o.restart_safe() is True


def test_active_statuses_block_restart():
    o = _orch()
    for i, s in enumerate(["in_progress", "working", "waiting_input"]):
        o._records[f"s{i}"] = _rec(f"s{i}", s)
    assert o.active_session_count() == 3
    assert o.restart_safe() is False


def test_mixed_counts_only_active():
    o = _orch()
    o._records["a"] = _rec("a", "idle")
    o._records["b"] = _rec("b", "in_progress")
    o._records["c"] = _rec("c", "done")
    assert o.active_session_count() == 1
    assert o.restart_safe() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_restart_safe.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'SessionOrchestrator' object has no attribute 'active_session_count'`.

- [ ] **Step 3: Write minimal implementation**

In `server/sessions/orchestrator.py`, add the constant after the import block (near the top, before the `SessionOrchestrator` class):

```python
# Statuses that mean a session is mid-turn or awaiting user input — restarting now
# would lose in-flight work. All other statuses (idle / ready_review / done /
# limited / ended / archived) are restart-safe: resume + transcript replay recover them.
_ACTIVE_STATUSES = frozenset({"in_progress", "working", "waiting_input"})
```

Add these two methods inside `class SessionOrchestrator` (place them near the other read-only accessors, e.g. just after `subscribe_events`/`unsubscribe_events`):

```python
    def active_session_count(self) -> int:
        """Number of sessions currently mid-turn or awaiting input (restart-blocking)."""
        return sum(1 for r in self._records.values() if r.status in _ACTIVE_STATUSES)

    def restart_safe(self) -> bool:
        """True when a restart now would interrupt no in-flight work."""
        return self.active_session_count() == 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_restart_safe.py -q -p no:cacheprovider`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_restart_safe.py
git commit -m "feat(orchestrator): restart_safe()/active_session_count() for deploy drain

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `/healthz` exposes `busy` / `activeSessions`

**Files:**
- Modify: `server/app.py` (the `healthz()` handler inside `create_app`)
- Test: `server/tests/test_healthz.py` (add one test)

**Interfaces:**
- Consumes: `app.state.orchestrator.active_session_count()` / `.restart_safe()` from Task 1.
- Produces: `/healthz` JSON body `{"status":"ok","sha":<str>,"busy":<bool>,"activeSessions":<int>}` (and `{"status":"schema_error","sha":<str>}` + 500 unchanged on DB failure).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_healthz.py`:

```python
def test_healthz_reports_busy_and_active_sessions(tmp_path):
    """/healthz surfaces orchestrator drain-readiness for the deployer's idle gate."""
    from server.sessions.types import SessionRecord
    client = _client(tmp_path)
    orch = client.app.state.orchestrator
    orch._records["s1"] = SessionRecord(
        id="s1", name="s1", account="acct", claude_session_id="cs",
        cols=80, rows=24, cwd="/tmp", status="in_progress",
    )
    with client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is True
    assert body["activeSessions"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_healthz.py::test_healthz_reports_busy_and_active_sessions -q -p no:cacheprovider`
Expected: FAIL with `KeyError: 'busy'`.

- [ ] **Step 3: Write minimal implementation**

In `server/app.py`, replace the `healthz()` handler body. Current:

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        try:
            async with _db.db_session() as db:
                await db.execute(_sa_select(_SessionModel).limit(1))
            return JSONResponse({"status": "ok", "sha": running_git_sha()})
        except Exception as exc:  # noqa: BLE001 — any DB/schema failure is unhealthy
            logging.getLogger("server").warning("healthz schema check failed", exc_info=exc)
            return JSONResponse({"status": "schema_error", "sha": running_git_sha()}, status_code=500)
```

New:

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Exercises every Session model column; a drifted DB raises → 500.
        orch = getattr(app.state, "orchestrator", None)
        active = orch.active_session_count() if orch is not None else 0
        try:
            async with _db.db_session() as db:
                await db.execute(_sa_select(_SessionModel).limit(1))
            return JSONResponse({
                "status": "ok", "sha": running_git_sha(),
                "busy": active > 0, "activeSessions": active,
            })
        except Exception as exc:  # noqa: BLE001 — any DB/schema failure is unhealthy
            logging.getLogger("server").warning("healthz schema check failed", exc_info=exc)
            return JSONResponse({"status": "schema_error", "sha": running_git_sha()}, status_code=500)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_healthz.py -q -p no:cacheprovider`
Expected: PASS (3 passed — the two existing plus the new one).

- [ ] **Step 5: Commit**

```bash
git add server/app.py server/tests/test_healthz.py
git commit -m "feat(healthz): expose busy/activeSessions for deploy drain gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `auto-deploy.sh` idle gate + defer marker

**Files:**
- Modify: `scripts/auto-deploy.sh` (add a `busy_verdict()` helper and an idle gate after the CI gate, before the pull)
- Test: `scripts/tests/auto-deploy-idle-gate.bats` (create)

**Interfaces:**
- Consumes: `/healthz` `busy` field (Task 2); seams `CCSWITCH_DEPLOY_BUSY`, `CCSWITCH_DEPLOY_DEFER_FILE`, `CCSWITCH_DEPLOY_MAX_DEFER_SEC`, plus the existing `CCSWITCH_DEPLOY_CI_OK` / `CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC`.
- Produces: a deploy that exits 0 without fast-forwarding while the server is busy (until the cap), writing/reading a marker file.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/auto-deploy-idle-gate.bats`:

```bash
#!/usr/bin/env bats
# Idle gate in scripts/auto-deploy.sh: a busy server defers the restart (no
# fast-forward) until idle or the max-defer cap. Uses the CCSWITCH_DEPLOY_BUSY
# seam (no real server) and CCSWITCH_DEPLOY_CI_OK=1 to pass the CI gate.

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"; DEPLOY="$TMP/deploy"
  git init -q "$SEED"
  git -C "$SEED" checkout -q -b master
  git -C "$SEED" config user.email t@example.com
  git -C "$SEED" config user.name tester
  mkdir -p "$SEED/scripts" "$SEED/web"
  cp "$SCRIPT" "$SEED/scripts/auto-deploy.sh"
  printf 'v1\n' > "$SEED/tracked.txt"
  printf '/.venv/\n' > "$SEED/.gitignore"
  git -C "$SEED" add -A
  git -C "$SEED" commit -qm c1
  git clone -q "$SEED" "$DEPLOY"
  git -C "$DEPLOY" config user.email t@example.com
  git -C "$DEPLOY" config user.name tester
  printf 'v2\n' > "$SEED/tracked.txt"
  git -C "$SEED" commit -qam c2
  MARKER="$TMP/defer-state"
}
teardown() { rm -rf "$TMP"; }

# busy => no fast-forward, marker written, exit 0
@test "busy server defers: no fast-forward, marker written" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1800 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"busy"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" != "$(git -C "$DEPLOY" rev-parse origin/master)" ]
  [ -f "$MARKER" ]
}

# busy but past the cap => force (reaches the stop-after-sync hook = fast-forwarded)
@test "busy past max-defer cap forces the deploy" {
  REMOTE="$(git -C "$DEPLOY" ls-remote "$SEED" refs/heads/master | cut -f1)"
  printf '%s 1\n' "$REMOTE" > "$MARKER"   # deferred since epoch=1 (ancient)
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1 \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"forcing"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

# idle => proceeds (reaches the stop-after-sync hook = fast-forwarded)
@test "idle server proceeds to deploy" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=0 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bats scripts/tests/auto-deploy-idle-gate.bats`
Expected: FAIL — without the gate a busy server still fast-forwards (test 1's HEAD≠origin assertion fails), and no marker is written.

- [ ] **Step 3: Write minimal implementation**

In `scripts/auto-deploy.sh`, immediately AFTER the CI-gate `case "$verdict" in … esac` block and BEFORE the `rollback_to_local()` definition, insert:

```bash
# Idle gate. A restart kills every live session, so defer while the running server
# is mid-turn (status in_progress/working/waiting_input). Skip-and-retry until idle,
# capped at CCSWITCH_DEPLOY_MAX_DEFER_SEC so a perpetually-busy session can't block
# deploys forever (a forced deploy falls back to resume). Placed BEFORE the pull so
# the "HEAD == origin/master ⇒ deployed" invariant holds (we never pull then defer).
# Test seam: CCSWITCH_DEPLOY_BUSY (1/0) forces the verdict without curling /healthz.
busy_verdict() {
  if [ -n "${CCSWITCH_DEPLOY_BUSY:-}" ]; then
    [ "$CCSWITCH_DEPLOY_BUSY" = "1" ] && echo busy || echo idle
    return 0
  fi
  local url="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
  local body; body="$(curl -fsS -m 3 "$url" 2>/dev/null || true)"
  # Unreachable / no busy field => treat as idle (recover a down server; the CI gate
  # already enforced correctness, and the post-restart health gate guards the rest).
  if grep -q '"busy":true' <<<"$body"; then echo busy; else echo idle; fi
}

if [ "$(busy_verdict)" = "busy" ]; then
  DEFER_FILE="${CCSWITCH_DEPLOY_DEFER_FILE:-$REPO/.git/ccswitch-deploy-defer}"
  MAX_DEFER="${CCSWITCH_DEPLOY_MAX_DEFER_SEC:-1800}"
  now="$(date +%s)"
  msha=""; mts=0
  if [ -f "$DEFER_FILE" ]; then read -r msha mts < "$DEFER_FILE" || true; fi
  if [ "$msha" != "$REMOTE" ]; then
    printf '%s %s\n' "$REMOTE" "$now" > "$DEFER_FILE"
    echo "[deploy] server busy — deferring ${REMOTE:0:9} (will retry)"; exit 0
  elif [ "$(( now - mts ))" -gt "$MAX_DEFER" ]; then
    echo "[deploy] server busy for $(( now - mts ))s (> ${MAX_DEFER}s) — forcing deploy of ${REMOTE:0:9}" >&2
  else
    echo "[deploy] server busy — deferring ${REMOTE:0:9} ($(( now - mts ))s so far)"; exit 0
  fi
fi
```

Then, so the marker is cleared once a deploy lands, add at the VERY END of the script (after the final `echo "[deploy] live + healthy at ${REMOTE:0:9}"`):

```bash
rm -f "${CCSWITCH_DEPLOY_DEFER_FILE:-$REPO/.git/ccswitch-deploy-defer}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `bats scripts/tests/auto-deploy-idle-gate.bats && bats scripts/tests/auto-deploy-dirty-tree.bats`
Expected: all green (3 idle-gate + 6 dirty-tree). The dirty-tree suite still passes because its `run_deploy` sets `CCSWITCH_DEPLOY_CI_OK=1` and does not set `CCSWITCH_DEPLOY_BUSY` → `busy_verdict` curls `CCSWITCH_PORT=0` → unreachable → `idle` → proceeds as before.

- [ ] **Step 5: Verify the script still parses**

Run: `bash -n scripts/auto-deploy.sh`
Expected: no output (syntax OK).

- [ ] **Step 6: Commit**

```bash
git add scripts/auto-deploy.sh scripts/tests/auto-deploy-idle-gate.bats
git commit -m "feat(deploy): defer restart while the server is mid-turn (idle gate)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification + PR

**Files:** none (verification only).

- [ ] **Step 1: Run the touched backend tests + lint**

Run:
```bash
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_orchestrator_restart_safe.py server/tests/test_healthz.py -q -p no:cacheprovider
/mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/ruff check src/ server/
PYTHONPATH=src:server /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/mypy server/ --ignore-missing-imports
```
Expected: tests pass, ruff clean, mypy success.

- [ ] **Step 2: Run both bats suites + syntax**

Run: `bash -n scripts/auto-deploy.sh && bats scripts/tests/`
Expected: all green.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --base master --title "deploy: defer restart while a session is mid-turn (Phase B)" --body "<summary of the three pieces + link to spec; note Phase A daemon is the follow-up>"
```

- [ ] **Step 4: Wait for CI green, then merge**

Run: `gh pr checks <num>` until `lint-and-test` + `frontend` pass, then `gh pr merge <num> --merge --delete-branch`.
Expected: merged; the auto-deployer (now with its own idle gate already on master after this lands) deploys it at the next idle window.

---

## Notes for the implementer

- `SessionRecord` (`server/sessions/types.py`) requires `id, name, account, claude_session_id, cols, rows, cwd`; everything else has defaults. Construct it with those kwargs in tests.
- `/healthz` is intentionally exempt from `IdentityMiddleware` (the deploy probes it unauthenticated) — do not add an identity dependency to it.
- The deploy script's existing CI gate and working-tree guard must remain ordered: fetch → CI gate → **idle gate (new)** → working-tree guard → pull. The idle gate must not pull.
- The full `server/tests` suite hangs in a firewalled sandbox (it polls real accounts; see the project memory). Run only the named test files locally; the full suite runs clean on GitHub CI.
