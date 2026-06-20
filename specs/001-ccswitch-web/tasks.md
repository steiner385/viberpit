# Tasks: ccswitch — Multi-Account Claude Session Manager (Web)

**Feature dir:** `specs/001-ccswitch-web/` · **Plan:** [plan.md](./plan.md) · **Constitution:** v1.1.0
**Tests:** included (Constitution Principle V mandates TDD + ≥85% coverage on critical paths).
**Stories:** US1 = backend session engine + API (MVP); US2 = mobile web UI. Plan phases P3/P4
(structured view, public-hosting hardening) are deferred — not enumerated here.

Paths are relative to repo root `cc-account-switcher/`.

---

## Phase 1: Setup

- [X] T001 Create monorepo scaffold: `server/` (Python package), `web/` (Vite app), keep existing `ccswitch/`; add root `README.md` run section
- [X] T002 [P] Backend deps + tooling in `server/pyproject.toml`: fastapi, uvicorn, sqlalchemy[asyncio], aiosqlite, alembic, cryptography, webauthn, pytest, pytest-asyncio, httpx, ruff, mypy
- [X] T003 [P] Frontend scaffold + deps in `web/`: Vite + React + TypeScript, Tailwind, shadcn/ui, @xterm/xterm + @xterm/addon-fit + @xterm/addon-accessibility, @tanstack/react-query, vitest, @testing-library/react, @playwright/test
- [X] T004 [P] Config module `server/config.py` reading env (host/port, `CCSWITCH_MASTER_KEY`, db URL, tailnet allowlist); add `.env.example`
- [X] T005 [P] `Dockerfile` (multi-stage: build `web/` static → serve from FastAPI) kept Railway-portable; `.dockerignore`
- [X] T006 [P] CI workflow `.github/workflows/ci.yml`: run `ruff`/`mypy`/`pytest` (server) and `tsc`/`vitest` (web) on push/PR

---

## Phase 2: Foundational (Plan Phase 0 — config-dir launch correctness) ⚠ BLOCKING

**Goal:** a real `claude` launches under a composed per-account config dir; tokens encrypted at rest.
Nothing in US1/US2 works until this completes. (research.md R1, R9)

- [X] T007 [P] Test: `add_account` captures the temp-login `.claude.json` to `store/<name>.claude.json` in `tests/test_accounts_claudejson.py`
- [X] T008 [P] Test: `compose` writes per-account `.claude.json` = account identity ⊕ shared allowlist (`mcpServers`/`projects`/`allowedTools`) in `tests/test_configdir_claudejson.py`
- [X] T009 [P] Test: `reconcile_links` symlinks only the share allowlist and keeps `.credentials.json`/`.claude.json`/`daemon.*`/`sessions`/`agents`/`tasks` real-per-account in `tests/test_configdir_isolation.py`
- [X] T010 [P] Test: refresh token round-trips through envelope encryption (encrypt→store→read→decrypt) in `tests/test_crypto.py`
- [X] T011 Implement `.claude.json` capture in `ccswitch/accounts.py::add_account`
- [X] T012 Implement per-account `.claude.json` compose (identity base ⊕ `store/claude.shared.json` allowlist) in `ccswitch/configdir.py`
- [X] T013 Rewrite `reconcile_links` share/isolate set in `ccswitch/configdir.py` (delete the old symlink-everything logic — Principle VI)
- [X] T014 Implement envelope encryption module `server/crypto/envelope.py` (AES-256-GCM, key from `CCSWITCH_MASTER_KEY`)
- [X] T015 Wire envelope encryption of refresh tokens into `ccswitch/creds.py` read/write (encrypt on write, decrypt in memory on read)
- [X] T016 LIVE SMOKE (Principle III): `CLAUDE_CONFIG_DIR=cfg/<acct> claude -p "ok"` returns success and `ccmax`-style `--resume` works across accounts; record result in `specs/001-ccswitch-web/quickstart.md` validation log

---

## Phase 3: US1 — Backend session engine + API (MVP)

**Goal:** an authenticated client can list/create/rename/close sessions across accounts, interact
live over a WebSocket, see live usage, and have sessions auto-rotate (wait-for-idle) on a limit.
**Independent test:** drive the full API + WS against the fake-`claude` PTY stub (no real quota);
verify create→interact→limit→rotate→continue and reconnect.

### Tests (write first)
- [X] T017 [P] [US1] Fake-`claude` PTY stub (scripted output, idle/limit/turn signals) in `tests/fakes/fake_claude_pty.py`
- [X] T018 [P] [US1] Test: Tailscale-identity middleware accepts allowlisted identity, rejects unidentified, on both HTTP and WS upgrade in `server/tests/test_auth.py`
- [X] T019 [P] [US1] Test: per-account usage poller returns authoritative usage, caches ≤60s, dedupes per account in `server/tests/test_usage.py`
- [X] T020 [P] [US1] Test: account selection honors soft reservation (no double-assign) + excludes limited/needs-relogin in `server/tests/test_selection.py`
- [X] T021 [P] [US1] Test: SessionManager spawns PTY, buffers bounded scrollback, replays on reattach in `server/tests/test_session_manager.py`
- [X] T022 [P] [US1] Test: wait-for-idle rotation — holds until idle, then kill+resume on next account, rebridges same id, max-wait fallback interrupts in `server/tests/test_rotation.py`
- [X] T023 [P] [US1] Test: REST contract (`/api/sessions`, `/api/accounts`, `/api/auth/me`, all-limited→409 earliestReset) in `server/tests/test_api.py`
- [X] T024 [P] [US1] Test: WS contract (attach/input/resize/interrupt ↔ output/status/usage/rotated/sessionEnded) in `server/tests/test_ws.py`
- [X] T025 [P] [US1] Test: no token/credential material in any log, response, or audit detail in `server/tests/test_redaction.py`

### Implementation
- [X] T026 [US1] SQLAlchemy async models `Session`, `AppSession`, `AuditEvent` in `server/db/models.py` + Alembic init migration
- [X] T027 [US1] DB session/engine + lifespan wiring in `server/db/__init__.py`
- [X] T028 [US1] Tailscale identity verifier (header / `tailscaled` whois) + allowlist in `server/auth/identity.py`
- [X] T029 [US1] Signed server-side session (AppSession) + middleware gating all routes + WS upgrade in `server/auth/middleware.py`; passkey/WebAuthn path scaffolded behind a disabled flag in `server/auth/webauthn.py`
- [X] T030 [US1] Secret-redaction log filter + audit-event writer in `server/audit.py`
- [X] T031 [US1] Per-account usage poller + in-memory pub/sub in `server/usage/poller.py` (wraps `ccswitch` core; authoritative signal, R4)
- [X] T032 [US1] Account selection + reservation glue in `server/sessions/selection.py` (reuses `ccswitch.selector`/ledger, FR-009a)
- [X] T033 [US1] `SessionManager`: PTY spawn under composed config dir, bounded scrollback ring, idle tracking in `server/sessions/manager.py`
- [X] T034 [US1] Wait-for-idle rotation in `server/sessions/rotation.py` (idle gate + limit trigger from poller/transcript-tail, kill+`--resume`, rebridge, repaint, max-wait fallback)
- [X] T035 [US1] WebSocket bridge `server/bridge/ws.py` (PTY↔client, control/event channel, output batching/backpressure per NFR-005)
- [X] T036 [P] [US1] REST router sessions in `server/api/sessions.py` (list/create/rename/close/get; 409+earliestReset)
- [X] T037 [P] [US1] REST router accounts in `server/api/accounts.py` (list+refresh usage / delete / relogin)
- [X] T038 [P] [US1] REST router auth + health in `server/api/auth.py`, `server/api/health.py`
- [X] T039 [US1] FastAPI app factory + middleware wiring + static-serve in `server/app.py`
- [X] T040 [US1] Verify ≥85% coverage on auth/selection/credential/rotation; fix gaps

**Checkpoint:** US1 independently testable — full API + WS green against the fake-`claude` stub.

---

## Phase 4: US2 — Mobile web UI (dashboard + terminal)

**Goal:** a mobile-first web app consuming US1: cross-account session list, live usage, create /
rename / close / switch, an xterm.js terminal session view, real-time updates, WCAG 2.1 AA on
management surfaces. **Independent test:** Playwright (mobile viewport) drives dashboard↔session
against a running US1 backend.

### Tests (write first)
- [X] T041 [P] [US2] Test: SessionList renders cross-account rows (name/account/status/usage), selection by keyboard+click in `web/src/components/SessionList.test.tsx`
- [X] T042 [P] [US2] Test: status shown by text+icon (not color alone); usage badge has numeric label + AA contrast tokens in `web/src/components/UsageBadge.test.tsx`
- [X] T043 [P] [US2] Test: rename/close/new dialogs + 44px targets + focus returns to list on close in `web/src/components/SessionActions.test.tsx`
- [X] T044 [P] [US2] Playwright: dashboard→open session→back; mobile viewport, no horizontal scroll; helper-key bar present in `web/e2e/dashboard.spec.ts`
- [X] T045 [P] [US2] Playwright: reconnect replays scrollback ≤10s; `rotated` event updates account badge in `web/e2e/rotation-reconnect.spec.ts`

### Implementation
- [ ] T046 [US2] Vite/React/Tailwind/shadcn base + theme tokens (AA contrast) + router in `web/src/main.tsx`, `web/src/App.tsx`
- [X] T047 [US2] API client (TanStack Query hooks) in `web/src/api/` + WS client (multiplexed channel) in `web/src/ws/client.ts`
- [X] T048 [US2] `SessionList` + `SessionCard` + `UsageStrip`/`UsageBadge` in `web/src/components/`
- [ ] T049 [US2] Session actions: new (optional initial prompt), rename, close (confirm) dialogs in `web/src/components/`
- [X] T050 [US2] Empty/first-run state + all-accounts-limited banner (earliest reset, local time) in `web/src/components/`
- [X] T051 [US2] `Terminal` view (xterm.js + fit addon) over WS, full-screen on phone with visible "back to list" in `web/src/components/Terminal.tsx`
- [X] T052 [US2] Mobile `HelperKeyBar` (Esc/Tab/Ctrl/arrows, ≥44px) in `web/src/components/HelperKeyBar.tsx`
- [X] T053 [US2] a11y: `aria-live="polite"` recent-output region + xterm accessibility addon + focus management (R6, NFR-004)
- [X] T054 [US2] Real-time wiring: status ≤3s, usage ≤60s, `rotated` badge update + non-blocking rotation notice (FR-019)
- [X] T055 [US2] Reconnect handling (re-attach + scrollback replay) in the WS client (SC-008)

**Checkpoint:** US2 independently testable — Playwright mobile pass green against US1.

---

## Phase 5: Polish & Cross-Cutting

- [ ] T056 [P] README run/deploy guide (local + Tailscale) + update `docs/SMOKE.md`
- [ ] T057 [P] Accessibility audit pass on management surfaces (axe/manual) against WCAG 2.1 AA
- [ ] T058 [P] Performance/backpressure validation: terminal streaming under burst + mobile-link throttle (NFR-005)
- [ ] T059 Security self-review: auth on every route+WS, secrets never logged, refresh tokens ciphertext at rest, audit coverage (Principle IV)
- [ ] T060 Run `quickstart.md` validation scenarios 1–9 end-to-end; record results
- [ ] T061 Finalize `Dockerfile` + env docs for the future Railway path (no public exposure yet)

---

## Dependencies & Order

- **Setup (P1)** → **Foundational (P2, BLOCKING)** → **US1 (P3)** → **US2 (P4)** → **Polish (P5)**.
- US2 depends on US1 (consumes its API/WS). US1 depends on P2 (sessions can't launch otherwise).
- Within US1: tests (T017–T025) → models/auth/usage/selection (T026–T032) → SessionManager/rotation/WS (T033–T035) → routers/app (T036–T039) → coverage (T040). T034 depends on T031/T033; T035 depends on T033.
- Within US2: tests (T041–T045) → base/clients (T046–T047) → components (T048–T053) → real-time/reconnect (T054–T055).

## Parallel Execution Examples

- **Setup:** T002, T003, T004, T005, T006 in parallel (distinct files).
- **P2 tests:** T007–T010 in parallel; then impl T011–T015 (T013 after T012; T015 after T014).
- **US1 tests:** T017–T025 all `[P]` (distinct test files) before implementation.
- **US1 routers:** T036, T037, T038 in parallel after the services they call exist.
- **US2 tests:** T041–T045 in parallel; **US2 components** T048–T053 largely parallel (distinct files).

## Implementation Strategy

- **MVP = Setup + Foundational + US1.** That yields a fully working, headless-testable backend
  (sessions across accounts, live interaction over WS, usage, wait-for-idle rotation) — demoable
  via the API/WS and the quickstart, with no UI yet.
- **Increment 2 = US2** delivers the mobile web experience on top of the MVP backend.
- **Deferred (separate task lists):** Plan P3 (structured claude.ai/code-style view via Agent SDK
  stream-json) and P4 (public-hosting hardening: enable passkey/WebAuthn, rate-limited login, audit
  UI, Railway deploy).
