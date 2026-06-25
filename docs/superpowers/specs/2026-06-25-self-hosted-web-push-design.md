# Self-Hosted Web Push Notifications — Design

**Date:** 2026-06-25
**Status:** Approved (design)
**Scope:** Add browser-native push notifications delivered by ccswitch itself (no
third-party service), targeting Android. Single, well-bounded feature — one spec → one
plan → one implementation cycle.

## Goal

Give the operator real phone notifications when a session needs them (transitions into
needs-you / crashed) or a session's PR breaks CI — delivered **by ccswitch itself** via
the W3C Web Push protocol, with **no external service** (no ntfy, Pushover, Discord).
This is the self-hosted resolution of the notification need that the dormant external
webhook sink (M2b, `CCSWITCH_PUSH_WEBHOOK_URL`) was built for but left unconfigured to
avoid publishing alert content to a third party.

**Target:** Android Chrome (subscribes from a normal browser tab; no home-screen install
required). iOS is explicitly out of scope (it requires an installed PWA + iOS 16.4+, a
larger UX surface) — see Out of Scope.

## Why this is the right shape

- **It's already a mobile-accessible web app.** ccswitch is a React SPA served by FastAPI
  (`mount_spa`) reachable from the phone over Tailscale. The missing pieces are a secure
  context, a service worker, and a push transport — not "a new app."
- **Self-hosted Web Push is more private, not less.** VAPID signs the push with the
  server's key; the payload is end-to-end encrypted to the browser's subscription keys.
  Google's push relay forwards ciphertext it cannot read. This is the technical reason it
  resolves the third-party-exposure concern that stopped the webhook approach.
- **It reuses the event detection already shipped.** M2b/M2b.2 already detect the two
  notify-worthy events and call `push()`. This feature adds a new *sink* behind the same
  call site — it does not add a second detection path.

## Prerequisite (one-time infra, NOT part of the code change)

Web Push and service workers require a **secure context (HTTPS)**. The app is currently
served plain-HTTP over Tailscale (`100.102.11.121:8787`). The tailnet has MagicDNS enabled
(`dobby.tail0dd570.ts.net`), so HTTPS is available via Tailscale's Let's Encrypt
integration:

```
tailscale serve --bg --https=443 http://127.0.0.1:8787
```

→ app reachable at `https://dobby.tail0dd570.ts.net`. Requires HTTPS enabled in the
tailnet admin console. This is documented as **step 0** of the rollout; it is operational,
done once, and gated by the operator (no code).

## Architecture

Five small units, each with one purpose and a clear interface.

### 1. Service worker — `web/public/sw.js`
A small static file (copied to `dist/` root by Vite → served at `/sw.js` at root scope).
Two handlers:
- `push` → parse the JSON payload `{title, body, url}` and call
  `self.registration.showNotification(title, {body, data:{url}, icon, tag})`.
- `notificationclick` → focus an existing app client if open, else
  `clients.openWindow(url)` to deep-link to the session.

`mount_spa` must serve `/sw.js` with `Content-Type: application/javascript` and must NOT
apply SPA index.html fallback to it (a service worker served as `text/html` is rejected by
the browser). This is the one SPA-serving change required.

### 2. VAPID keypair — `server/config.py` + a keygen helper
Application Server Identity for Web Push. Generated **once** by a keygen helper that lives
with the sink in `server/sessions/push.py` (invoked as `python -m server.sessions.push
--keygen`), which prints the pair to paste into the env file:
- `CCSWITCH_VAPID_PRIVATE_KEY` and `CCSWITCH_VAPID_PUBLIC_KEY` in
  `/etc/ccswitch/ccswitch.env` (root-owned; same place as other secrets).
- `CCSWITCH_VAPID_SUBJECT` (a `mailto:` or the app URL) — required by the spec.
- `config.py` reads them as `str | None`; absence => push disabled (no-op), mirroring how
  `PUSH_WEBHOOK_URL` is read today.

### 3. Subscription store — new table `push_subscriptions`
A browser PushSubscription, persisted. **All new columns nullable** (dev SQLite `init_db`
backfills; no ALTER) per the repo migration rule.
- Columns: `endpoint` (unique), `p256dh`, `auth`, `identity` (the Tailscale identity that
  subscribed, for future targeting), `created_at` (via `iso_utc`, never bare isoformat).
- v1 has no per-identity targeting — every subscription receives every push — but the
  column is stored so targeting is a later, non-migrating change.

### 4. API — `server/api/push.py`
- `GET /api/push/config` → `{vapidPublicKey, enabled}` (enabled = VAPID configured). The
  frontend needs the public key to subscribe.
- `POST /api/push/subscribe` → body = the PushSubscription JSON; **upsert** by `endpoint`
  (idempotent re-subscribe). 201/200.
- `DELETE /api/push/subscribe` → body/query `{endpoint}`; remove. Idempotent (404-tolerant).
- All under the existing Tailscale-identity auth.

### 5. Web Push sink — extends `server/sessions/push.py`
**Consolidation (constitution VI — no parallel implementations):** today `push()` *is* the
notification dispatcher (a single webhook sink). Generalize it into a `notify(title, body,
*, url=None)` dispatcher that fans out to **both** sinks:
- the existing **webhook** sink (`PUSH_WEBHOOK_URL`, still dormant unless configured), and
- the new **web-push** sink: load all `push_subscriptions`, send each a VAPID-signed,
  encrypted push via **`pywebpush`** (new vetted dependency, constitution VII).

The M2b/M2b.2 event-detection call sites change only from calling `push(...)` to calling
`notify(...)` — one dispatcher, two sinks, one detection path. Both sinks are
fire-and-forget and never raise into a caller.

### Frontend UI
A single **"Enable notifications"** toggle, placed in the existing settings surface (the
accounts/settings panel) — deliberately **not** a new hook in `Dashboard.tsx` (avoids the
documented partial-mocks test-fragility trap). On enable: `GET /api/push/config` → register
`/sw.js` → `Notification.requestPermission()` → `pushManager.subscribe({userVisibleOnly:
true, applicationServerKey: <vapidPublicKey>})` → `POST /api/push/subscribe`. On disable:
`subscription.unsubscribe()` + `DELETE`. The toggle reflects state: `granted` / `denied` /
`unsupported` / `insecure-context`.

## Data flow

**Enable:**
```
browser → register sw.js → Notification.requestPermission()
       → pushManager.subscribe(applicationServerKey = VAPID public)
       → PushSubscription → POST /api/push/subscribe → row upserted
```

**Notify:**
```
session→needs-you/crashed  OR  PR→broke CI   (existing M2b/M2b.2 detector)
   → notify(title, body, url)            (the single dispatcher)
       ├─ webhook sink   (no-op unless PUSH_WEBHOOK_URL set)
       └─ web-push sink  → for each subscription: pywebpush.send(sub, payload, VAPID)
                         → Google push service (relays ciphertext)
                         → SW 'push' event → showNotification
                         → tap → notificationclick → open/focus the session deep link
```

## Error handling

Fire-and-forget throughout — matches today's `push()` contract (never raises into the
event path; a notification failure must never break session handling).
- `pywebpush` send returns **404 / 410 Gone** → the subscription is dead → **delete it**
  from the store (standard Web Push pruning).
- Other send errors (network, 5xx) → log at warning (throttled), leave the subscription.
- VAPID not configured **or** zero subscriptions → silent no-op (`enabled:false`).
- SW registration fails / permission denied / non-secure context → the toggle surfaces the
  state; no exception escapes to the UI.

## Testing (TDD)

**Backend (pytest):**
- `subscribe` upserts by endpoint (re-subscribe is idempotent); `unsubscribe` deletes and
  is 404-tolerant.
- web-push sink: loads subscriptions and calls an **injected send-fn** (mirroring the
  existing injectable `opener` in `push.py`) with the correct encrypted payload + VAPID
  args; a **410** from the send-fn prunes that subscription row.
- `notify()` dispatcher fans out to both sinks; with no VAPID/subscriptions it no-ops; with
  no webhook URL the webhook sink no-ops — neither raises.
- `GET /api/push/config` returns the public key and `enabled` correctly.
- New column policy: `push_subscriptions` columns are nullable; `init_db` backfill verified.

**Frontend (vitest):**
- The toggle component across permission states (`granted`/`denied`/`unsupported`/
  `insecure`) with a mocked `serviceWorker`/`PushManager`; on enable it POSTs the
  subscription; on disable it unsubscribes + DELETEs. Types kept optional/additive so the
  full fixture suite is unaffected.
- Service-worker handler logic (`push`→payload→showNotification args; `click`→openWindow
  url) extracted to a pure, unit-testable function where practical.

**Verification gates (project-standard):** backend `ruff` + `mypy` + `pytest`; web `vitest`
+ `tsc -b` + `eslint --max-warnings 0`. New dep `pywebpush` added to the `[server]` extra
with the lockfile committed (CI runs `pip install -e ".[server]"`).

## Out of scope (deferred / YAGNI for a single Android user)

- **iOS support** — requires an installed PWA (Add to Home Screen) + iOS 16.4+; a distinct
  UX surface. Add later if needed.
- **Installable PWA manifest + offline caching** — Android receives push from a normal tab
  with only the service worker, so no `manifest.webmanifest` / Workbox offline shell in v1.
  Noted as a clean future add (would make the app installable).
- **Per-event preferences / topics / per-identity targeting** — v1 sends all events to all
  subscriptions. The `identity` column is stored so targeting is a later non-migrating
  change.
- **Reworking the dashboard UI to be mobile-first** — separate effort.

## Single-source-of-truth obligations

- One notification dispatcher (`notify()`); the webhook and web-push sinks live behind it.
  Do not introduce a second path or duplicate the event detection.
- Reuse `server/time_iso.py:iso_utc` for `created_at` (never bare `.isoformat()`).
- New session/HTTP wire fields (if any) must update both serializers if they ride the
  session payload — but this feature's API is standalone (`/api/push/*`), so it does not
  touch the session dual-serializer.
