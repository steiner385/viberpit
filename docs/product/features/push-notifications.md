---
slug: push-notifications
title: Push & Web Notifications
state: live
links:
  code:
    - server/api/push.py
    - server/api/notifications.py
    - server/sessions/push.py
    - server/sessions/push_store.py
    - server/sessions/notify_rules.py
    - web/src/api/push.ts
    - web/src/components/settings/NotificationsSection.tsx
    - web/src/components/settings/NotificationsToggle.tsx
  prs: [236, 243, 281, 277, 278, 288, 606, 684, 411]
---

# Push & Web Notifications

**Problem** — The operator can't watch the dashboard all the time. When a session finishes a
turn ("your turn"), needs attention, or a session's PR breaks CI, they need to be alerted on
their device so they can come back — rather than polling the UI.

**Target users** — The single operator, away from the dashboard (phone locked, another tab,
different room), who wants to be pulled back only when a session actually needs them.

**Desired outcome** — Self-hosted Web Push (VAPID) delivers a notification to the operator's
subscribed browser/phone for high-value events, with server-side routing rules (event
class, per-repo scope, quiet hours) so the operator isn't spammed. Tapping a notification
deep-links to the relevant session.

**Success metrics** — (unknown — needs owner input). Code affords: a routing-preview
"test"/dry-run endpoint over the pure `notify_rules` engine, so the operator can validate a
rule fires as expected before relying on it.

**Non-goals** — Third-party push vendors as the primary path (the top-tier "needs-you" path
can fan out via an external webhook sink, but core delivery is self-hosted Web Push to avoid
the prod insecure-context constraint). Notifying anyone but the tailnet-authenticated
operator. Using the push deep-link as a webhook target (explicitly guarded against, #288).

**Notes** — Web Push shipped self-hosted for Android (#281) after a written spec and plan
(#277, #278); VAPID keys are generated server-side and the public key is served to the
client to subscribe. Server-side webhook delivery happens at the `notify()` call site to
avoid the browser insecure-context limitation. Notification channels, routing rules, and
quiet hours are CRUD models exposed via `/api/notifications` and configured in the Settings
UI (#606 power-user config suite). Specific alert types wired up: top-tier needs-you push
(#236), and a session's PR breaking CI (#243). Also surfaces a "git credential refresh
needed" notification (#684).

**Evidence** —
- Subscription API + VAPID: `server/api/push.py`, web client `web/src/api/push.ts`
  (`enablePush` registers `/sw.js`, subscribes with the VAPID key).
- Delivery + rules: `server/sessions/push.py`, `server/sessions/push_store.py`,
  `server/sessions/notify_rules.py`, `server/api/notifications.py` (channels, rules, quiet
  hours, routing dry-run).
- Settings UI: `web/src/components/settings/NotificationsSection.tsx`,
  `web/src/components/settings/NotificationsToggle.tsx`.
- PRs: #281 (self-hosted Web Push, Android), #277/#278 (spec/plan), #236 (needs-you push via
  webhook sink), #243 (alert on session PR CI break), #606 (notifications config suite),
  #684 (git-credential-refresh notification), #288 (fix: stop push deep-link being used as
  webhook target).
