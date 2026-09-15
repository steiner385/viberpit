---
slug: web-dashboard-mobile
parent: operator-experience
title: Web Dashboard & Mobile Access
state: live
links:
  code:
    - web/src/App.tsx
    - web/src/shell/AppShell.tsx
    - web/src/shell/useViewport.tsx
    - web/src/features/sessions/list/SessionCard.tsx
    - web/src/features/sessions/list/DispatchBar.tsx
    - web/src/features/sessions/list/useRepoSuggestion.ts
    - web/src/api/repoCheck.ts
    - web/src/features/sessions/SessionBody.tsx
    - web/src/features/sessions/composer/Composer.tsx
  prs: [423, 461, 498, 776, 778, 316, 313, 330, 339, 524, 566, 376, 1471]
---

# Web Dashboard & Mobile Access

Monitor accounts and sessions and steer coding work comfortably from a desktop browser or phone.

**Problem** — Running and switching between several live `claude` sessions across multiple
Max accounts from a terminal ties you to one machine. There is no way to glance at account
usage, spot a session that needs your input, or type a follow-up prompt from a phone while
away from the desk.

**Target users** — The single operator running ViberPit on their tailnet: at the desk on a
wide screen, and on a phone or tablet anywhere on the Tailscale network. A wall-mounted kiosk
display is a fourth, unattended "user" (see Modes below).

**Desired outcome** — A browser UI, reachable from any device on the tailnet (via
`tailscale serve`), that shows a dashboard of accounts and their usage, a session list with
live status, a transcript for each session, and a prompt composer — all usable one-handed on
a phone. One shell (`AppShell`) and one route table drive every device width and mode, rather
than a mobile/desktop fork with its own copy of the rules.

**Modes** — one operator wears several hats, so the nav is organized around the question each
hat is asking rather than around a per-user account
(`docs/superpowers/specs/2026-09-08-finish-line-persona-modes-design.md` D1, §3):

- **Developer / vibe coder** — "What needs me right now? What are my sessions and PRs doing?"
  Primary surfaces: Sessions (composer, permission cards, the Background scope) and Pipeline.
- **Product manager** — "What are we building, how does it fit together, in what order?"
  Primary surface: Roadmap (portfolio → product → capability → initiative → issues, plus the
  Inbox scope for proposals).
- **Project manager** — "What is in flight, blocked, or waiting on a human? Is delivery
  healthy?" Primary surfaces: Roadmap's Needs-you tabs and Delivery (automations, CI health).
- **Operator / admin** — "Accounts, hosts, deploy, config." Primary surface: Settings (17
  sections across 5 groups — `web/src/features/settings/settingsDefs.ts` — split variant).

These are information-architecture groupings, not accounts or permission levels — every mode
is reachable from the same five-entry nav rail at all times.

Independently of persona, the shell also has a **kiosk mode** (`?kiosk=1`) for an unattended
wall display: `web/src/shell/useViewport.tsx` reports it as a `LayoutMode`, and
`web/src/shell/AppShell.tsx`'s region-visibility matrix (`REGION_VISIBILITY`) removes the nav
rail, session list, composer and drawer entirely, refuses every `ui/Dialog`, and enlarges the
whole type scale (`--scale`) for ten-foot readability. Only the two danger-tone banners a
passer-by must act on (daemon down, every account rate-limited) stay visible.
`shell/navDefs.ts`'s `showInKiosk` flag gates which sections a kiosk URL may ever land on — the
bar is "verified read-only", not "looks glanceable" (see `docs/ACCESSIBILITY.md` §2 for the
full region/mode table). Kiosk is orthogonal to the four persona modes above: today only
Pipeline is kiosk-safe.

Independently of both, `useViewport()` also reports a width tier (`WIDTH_TIERS`:
phone/tablet/desktop/wide). It is a *finer* axis than `LayoutMode` and gates far less: master-detail
vs. single-band rendering is the `LayoutMode`'s call, made by `REGION_VISIBILITY` in `AppShell.tsx`
(`app.list` stays mounted beside an open item on `desktop`, and gives up the one band to it on
phone/tablet), and touch targets are the unconditional `min-h-target` token rather than anything
width-dependent. The non-test readers of the tier itself are
`shell/regions/NavRail.tsx` (the rail may expand only at `wide`), `shell/regions/ContextList.tsx`
(the list column is resizable only at `desktop`/`wide`) and `ui/list/CardFrame.tsx` (swipe gestures
are live only below `desktop`). See `docs/ACCESSIBILITY.md` §2 for the full region/mode
table.

**Success metrics** — (unknown — needs owner input). Observable proxies in code: session
cards reflect live status without sticking on "Working…"; layout adapts across widths via
`useViewport`'s width tiers; safe-area insets are respected on notched phones; no dropped
interrupts/answers on reconnect.

**Non-goals** — Public internet exposure (the server binds loopback and is fronted by
`tailscale serve`; public hosting/passkey auth is a later phase). Multi-user/multi-tenant
UI. Native mobile apps — this is a responsive web app.

**Notes** — Mobile ergonomics are a heavily-churned, first-class concern, not an
afterthought: dedicated PRs cover mobile-responsive account cards and session action
buttons with overflow menus (#778, #776), swipe-to-archive/delete on cards (#313, #316),
Android soft-keyboard handling and viewport shrinking (#330, #339), a mobile-reachable tab
strip with count badges (#461), safe-area insets on edge-touching elements (#524), and a
static header/footer with only the session list scrolling (#376). A test invariant locks
the mobile/desktop single-source-of-truth so the layouts can't diverge (#423,
`web/src/shell/visibilityMatrix.test.tsx`). #1471 (finish-line row 7) retired the
`Dashboard`/`DesktopShell` fork this doc used to describe in favour of the one `AppShell`
above, moved every component listed here out of `components/` into its `features/`/`shell/`
home, and replaced the ad-hoc `lib/useMediaQuery.ts` call sites with the single
`useViewport()` reader. The session transcript is a live view bridged over WebSocket, not a
raw PTY dump — there is no terminal emulator in the tree.

**Dispatch-bar prompt checks** — two fail-open helpers sit on the new-session prompt in
`DispatchBar.tsx`. The ✨ *Assess and refine* button asks `POST /api/dispatch/assess` for a
model tier and a refined prompt, shown as the "Refined:" chip with Accept / Discard.

*Wrong-repository check* — a prompt about KinDash dispatched to `steiner385/viberpit` used to
start a session in the wrong checkout. Now, once the draft is ≥ 30 characters and has been idle
for 1.5 s, the bar asks `POST /api/dispatch/repo-check` `{prompt, repo}`
(`web/src/api/repoCheck.ts`, `useRepoSuggestion.ts`); results are cached per draft text so the
same prompt is never re-asked. When the server is confident the prompt belongs elsewhere a card
in the Refined chip's visual language (`role="status"`) appears under the textarea: "Looks like
**cairnea/KinDash** work — *reason*", with **Switch to cairnea/KinDash** (selects and persists
that repo, even one not in the Repository list) and **Keep <current repo>** (dismisses for this
draft; typing new text re-arms). Enter / Create also runs the check first, waiting at most 3 s:
a positive answer *holds* the dispatch — the card is prefixed "Not created yet — " and either
button finishes the dispatch with the chosen repo — while a null, an error, or a timeout
dispatches immediately. Kill switch: `VIBERPIT_DISPATCH_REPO_CHECK_ENABLED` (default on) on the
edge; off, the endpoint answers "no suggestion" and the UI stays silent.

**Evidence** —
- Shell and layout: `web/src/shell/AppShell.tsx` (regions + mode matrix), `web/src/shell/useViewport.tsx`
  (the one layout `matchMedia` reader), `web/src/shell/routes.tsx` (the one route table —
  `web/src/App.tsx` is a 41-line entry point that mounts it).
- Session UI: `web/src/features/sessions/list/SessionCard.tsx`,
  `web/src/features/sessions/SessionBody.tsx` (transcript host),
  `web/src/features/sessions/composer/Composer.tsx`, `web/src/features/sessions/list/DispatchBar.tsx`.
- Responsiveness: `web/src/shell/useViewport.tsx`; swipe test
  `web/src/features/sessions/list/SessionCardSwipe.test.tsx`.
- The repo-root `README.md`'s `## ViberPit web` section (Tailscale-served, phone/tablet access).
  `web/README.md` is the frontend developer guide — folders, gates, conventions — with no Tailscale
  prose.
- PRs: #423 (mobile/desktop single-source invariant), #461 (mobile-reachable tab strip),
  #776/#778 (mobile-responsive session/account controls), #313/#316 (swipe-to-archive),
  #330/#339 (Android keyboard/soft-keyboard), #524 (safe-area insets), #498 (mobile "more"
  toggle in DispatchBar), #566 (session-card badge/action layout), #1471 (`AppShell` unification,
  the four modes above, `components/` retired).
