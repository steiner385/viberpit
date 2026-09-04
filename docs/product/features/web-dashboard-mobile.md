---
slug: web-dashboard-mobile
title: Web Dashboard & Mobile Access
state: live
links:
  code:
    - web/src/App.tsx
    - web/src/components/Dashboard.tsx
    - web/src/components/DesktopShell.tsx
    - web/src/components/SessionCard.tsx
    - web/src/components/DispatchBar.tsx
    - web/src/components/session/SessionView.tsx
    - web/src/components/session/Composer.tsx
    - web/src/lib/useMediaQuery.ts
  prs: [423, 461, 498, 776, 778, 316, 313, 330, 339, 524, 566, 376]
---

# Web Dashboard & Mobile Access

**Problem** — Running and switching between several live `claude` sessions across multiple
Max accounts from a terminal ties you to one machine. There is no way to glance at account
usage, spot a session that needs your input, or type a follow-up prompt from a phone while
away from the desk.

**Target users** — The single operator running ccswitch on their tailnet: at the desk on a
wide screen, and on a phone or tablet anywhere on the Tailscale network.

**Desired outcome** — A browser UI, reachable from any device on the tailnet (via
`tailscale serve`), that shows a dashboard of accounts and their usage, a session list with
live status, an inline terminal/transcript for each session, and a prompt composer — all
usable one-handed on a phone. The same single source of truth drives both the wide-screen
master-detail `DesktopShell` and the mobile stacked layout.

**Success metrics** — (unknown — needs owner input). Observable proxies in code: session
cards reflect live status without sticking on "Working…"; layout adapts across widths via
container queries; safe-area insets are respected on notched phones; no dropped
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
the mobile/desktop single-source-of-truth so the two layouts can't diverge (#423). The
inline terminal is a live transcript view (xterm-style) bridged over WebSocket, not a raw
PTY dump.

**Evidence** —
- Layouts: `web/src/components/DesktopShell.tsx` (wide-screen master-detail),
  `web/src/components/Dashboard.tsx`, `web/src/App.tsx`.
- Session UI: `web/src/components/SessionCard.tsx`,
  `web/src/components/session/SessionView.tsx`, `web/src/components/session/Composer.tsx`,
  `web/src/components/DispatchBar.tsx`.
- Responsiveness: `web/src/lib/useMediaQuery.ts`; swipe test
  `web/src/components/__tests__/SessionCardSwipe.test.tsx`.
- README `## ccswitch web` section (Tailscale-served, phone/tablet access).
- PRs: #423 (mobile/desktop single-source invariant), #461 (mobile-reachable tab strip),
  #776/#778 (mobile-responsive session/account controls), #313/#316 (swipe-to-archive),
  #330/#339 (Android keyboard/soft-keyboard), #524 (safe-area insets), #498 (mobile "more"
  toggle in DispatchBar), #566 (session-card badge/action layout).
