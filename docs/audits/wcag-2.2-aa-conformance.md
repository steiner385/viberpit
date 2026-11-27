# WCAG 2.2 AA conformance record

Status: current as of 2026-11-27. Owner: G18, #1416 (part of epic #1395).

The benchmark is **WCAG 2.2 Level AA** — already the constitution's bar (v1.2.0, "UI standards"),
so this record needs no governance amendment. It lists every Level A and AA success criterion, what
is known about it, and how it is known.

**This is not a conformance claim.** No criterion below has been verified with a screen reader, a
physical phone or a person who relies on assistive technology. §3 is the protocol for that and its
results table is empty. Until it is filled in, the honest summary is: the automated half is in
place and passing, the four gaps it found have since been fixed, and the assistive-technology half is not done.

## 1. How to read the status column

| Status | Means | Does not mean |
|---|---|---|
| **Automated** | A named test fails if this regresses, on the routes that test covers. | That every screen is covered — the gates cover the five top-level routes on a seeded backend. |
| **Partly automated** | A tool checks the part of the criterion a tool can decide; the rest needs a person. | That the criterion is met. |
| **Inspected** | Read in the source during this audit (2026-11-27). No regression test. | That it will stay true. |
| **Gap** | A known failure, with its issue. | — |
| **Not verified** | Nobody has checked. | That it fails. |
| **N/A** | The content the criterion governs does not exist in the app. | — |

The **AT** column says whether the criterion can only be confirmed with assistive technology or a
real device (§3). Every "needed" is currently **not done**.

## 2. The matrix

Paths are relative to the repo root. "Matrix gate" is `web/e2e/cross-browser/shell-modes.spec.ts`
(axe in the real engine, every route × four layout modes); "adaptation gate" is
`web/e2e/cross-browser/wcag-adaptations.spec.ts`. Both run on Chromium, Firefox and WebKit in CI.

### Principle 1 — Perceivable

| SC | Name | Level | Status | Evidence, and what is left | AT |
|---|---|---|---|---|---|
| 1.1.1 | Non-text Content | A | Partly automated | Matrix gate: axe `image-alt`, `button-name`, `svg-img-alt`. Left: whether the alternatives are *useful* — a rendered Mermaid diagram's alternative is the fixed string "Mermaid diagram" (`web/src/features/sessions/transcript/MermaidDiagram.tsx`). | needed |
| 1.2.1 | Audio-only and Video-only (Prerecorded) | A | N/A | No audio or video playback in `web/src` (microphone capture for dictation is input). | — |
| 1.2.2 | Captions (Prerecorded) | A | N/A | As 1.2.1. | — |
| 1.2.3 | Audio Description or Media Alternative | A | N/A | As 1.2.1. | — |
| 1.2.4 | Captions (Live) | AA | N/A | As 1.2.1. | — |
| 1.2.5 | Audio Description (Prerecorded) | AA | N/A | As 1.2.1. | — |
| 1.3.1 | Info and Relationships | A | Partly automated | Matrix gate (axe structure rules); one `<main>`, named landmarks and one `<h1>` per page (`web/src/features/settings/pageContract.test.tsx`, `web/src/shell/AppShell.test.tsx`). Two accepted ARIA deviations, L2 and L4 in `docs/product/ui-a11y-analyzer-limitations.md`. | needed |
| 1.3.2 | Meaningful Sequence | A | Not verified | Reading order is DOM order; nobody has listened to it. | needed |
| 1.3.3 | Sensory Characteristics | A | Not verified | Needs a read of instructional copy for "the button on the right" phrasing. | — |
| 1.3.4 | Orientation | AA | Automated | Matrix gate runs phone-portrait and phone-landscape; no orientation lock in `web/src/pwa/manifest.ts` or in script. Left: a real rotation, mid-task. | needed |
| 1.3.5 | Identify Input Purpose | AA | Inspected | The one field that identifies the user — the optional identity on the passkey sign-in page — declares `autocomplete="username webauthn"` (`web/src/features/auth/LoginPage.tsx`). No other field collects personal information. | — |
| 1.4.1 | Use of Color | A | Partly automated | `StatusPill` pairs icon and text (`web/src/ui/Pills.test.tsx`). The current nav item is a fill plus a colour change only (`web/src/shell/regions/NavItem.tsx`); in forced colours it now gets an outline (adaptation gate). Left: the same question for every other "selected" state. | — |
| 1.4.2 | Audio Control | A | N/A | As 1.2.1. | — |
| 1.4.3 | Contrast (Minimum) | AA | Automated | `web/src/__tests__/contrast.test.ts` (every token pair, both themes); matrix gate (axe `color-contrast` on rendered pages, light theme). Left: rendered dark theme is covered by arithmetic only. | — |
| 1.4.4 | Resize Text | AA | Automated | Adaptation gate: 200% browser zoom (640×400) and text-only 200% on all five routes — nothing off-screen, clipped or squeezed out. | needed |
| 1.4.5 | Images of Text | AA | Inspected | Only the logo (exempt) and Mermaid diagrams, which are rendered images containing text; the source is shown only if rendering fails. Open question, not a decided pass. | — |
| 1.4.10 | Reflow | AA | Automated | Adaptation gate: all five routes hold at 320×568 and at 320×256 (400% of 1280×1024). Below 360px of height (`tiny:`, `index.css`, #3561) the Sessions dock is capped at 34vh and scrolls, and Pipeline scrolls as one column with a 12rem floor under the board. | needed |
| 1.4.11 | Non-text Contrast | AA | Not verified | No tool here measures control borders, icons or the focus ring against their backgrounds. | — |
| 1.4.12 | Text Spacing | AA | Automated | Adaptation gate: the criterion's four overrides on a 390px phone, all five routes. | — |
| 1.4.13 | Content on Hover or Focus | AA | Inspected | One custom tooltip (`web/src/shell/regions/NavItem.tsx`): Escape dismisses it, it persists until blur or mouse-leave; "hoverable" is inferred from the markup, not tested. Native `title` tooltips are user-agent controlled. | — |

### Principle 2 — Operable

| SC | Name | Level | Status | Evidence, and what is left | AT |
|---|---|---|---|---|---|
| 2.1.1 | Keyboard | A | Partly automated | `web/e2e/keyboard-nav.spec.ts`; adaptation gate tabs through every route. Left: every action on every screen, and a hardware keyboard on iOS and Android. | needed |
| 2.1.2 | No Keyboard Trap | A | Inspected | `Dialog` traps focus by design and releases on Escape (`web/src/ui/Dialog.tsx`). No test proves the absence of an accidental trap. | needed |
| 2.1.4 | Character Key Shortcuts | A | Automated | The Roadmap inbox's single-key shortcuts run from the list's own key handler, so they are active only while it has focus (`web/src/features/roadmap/inbox/InboxScopeBody.test.tsx`; was page-wide, fixed by #3562). Session-list keys were already focus-scoped. Left: a pass with speech input. | needed |
| 2.2.1 | Timing Adjustable | A | Automated | Archive and triage Undo toasts no longer dismiss themselves (`web/src/api/__tests__/triageHooks.test.tsx`; were 6–8 s, fixed by #3563). Plain notification toasts still time out, and carry no action. | needed |
| 2.2.2 | Pause, Stop, Hide | A | Partly automated | Adaptation gate: nothing animates under `prefers-reduced-motion`, with a control showing the same page does animate otherwise. Left: there is no pause for someone who has not set that preference, and the kiosk attract cycle (`web/src/shell/useAttractCycle.ts`) has none at all. | — |
| 2.3.1 | Three Flashes or Below Threshold | A | Inspected | Motion is a pulse, a spinner and a shimmer; nothing flashes. | — |
| 2.4.1 | Bypass Blocks | A | Automated | Matrix gate (axe `bypass`, `region`): landmarks and headings are the mechanism. There is no skip link. | needed |
| 2.4.2 | Page Titled | A | Automated | `web/src/features/settings/pageContract.test.tsx` asserts `document.title` per page. | — |
| 2.4.3 | Focus Order | A | Not verified | The adaptation gate records the order but nothing judges whether it makes sense. | needed |
| 2.4.4 | Link Purpose (In Context) | A | Partly automated | Matrix gate (axe `link-name`). Left: whether the names are meaningful out of context. | needed |
| 2.4.5 | Multiple Ways | AA | Inspected | Section nav plus the command palette (`web/src/features/palette/AppPalette.tsx`, Ctrl+K). The palette has no visible button, so a pointer-only user has one way. | — |
| 2.4.6 | Headings and Labels | AA | Partly automated | One `<h1>` per page (page contract); axe `label`, `empty-heading`. Left: whether they describe the content. | needed |
| 2.4.7 | Focus Visible | AA | Automated | Adaptation gate: every tab stop on every route paints differently when focused — and, in forced colours, has a real outline. Fixed here: it was 0 of 81 stops in forced colours (`web/src/index.css`). | needed |
| 2.4.11 | Focus Not Obscured (Minimum) | AA | Automated | Adaptation gate at 1280×800: no focused control is wholly covered. Left: a phone with the software keyboard up, where the dock and keyboard compete. | needed |
| 2.5.1 | Pointer Gestures | A | Inspected | Card swipe repeats in the row menu; the browser tab's pinch has zoom buttons (`web/src/lib/useSwipeAction.ts`). | needed |
| 2.5.2 | Pointer Cancellation | A | Inspected | No action fires on pointer-down; down handlers only start a drag or close a menu. | — |
| 2.5.3 | Label in Name | A | Automated | Matrix gate (axe `label-content-name-mismatch`); one real failure was fixed under #1412. | needed |
| 2.5.4 | Motion Actuation | A | N/A | No device-motion or orientation input. | — |
| 2.5.7 | Dragging Movements | AA | Automated | The sidebar resizer toggles narrow/wide on double-click, with Narrow/Wide buttons in Settings › Appearance, beside its arrow keys (`web/src/features/sessions/list/SidebarResizer.test.tsx`; fixed by #3564). Other drags already had an alternative. | needed |
| 2.5.8 | Target Size (Minimum) | AA | Automated | Matrix gate (24px, and the app's own 44px on touch) and `web/e2e/touch-targets.spec.ts`. Left: real fingers. | needed |

### Principle 3 — Understandable

| SC | Name | Level | Status | Evidence, and what is left | AT |
|---|---|---|---|---|---|
| 3.1.1 | Language of Page | A | Automated | Matrix gate (axe `html-has-lang`); `web/index.html` declares `lang="en"`. | — |
| 3.1.2 | Language of Parts | AA | Not verified | The interface is English. Transcript content in another language is not marked. | needed |
| 3.2.1 | On Focus | A | Not verified | — | needed |
| 3.2.2 | On Input | A | Not verified | — | needed |
| 3.2.3 | Consistent Navigation | AA | Inspected | Navigation is the shell's, not each page's (`web/src/shell/visibilityMatrix.test.tsx` pins which region exists in which mode). | — |
| 3.2.4 | Consistent Identification | AA | Not verified | — | — |
| 3.2.6 | Consistent Help | A | Inspected | Help is rendered by the shell in the same place on every page (`web/src/shell/regions/GlobalActions.tsx`). | — |
| 3.3.1 | Error Identification | A | Inspected | `web/src/ui/Field.tsx` renders the error as `role="alert"` and wires it to the control with `aria-describedby` (`web/src/ui/FormControls.test.tsx`). `aria-invalid` is the caller's job, so it can be forgotten. | needed |
| 3.3.2 | Labels or Instructions | A | Partly automated | Matrix gate (axe `label`, `select-name`). | needed |
| 3.3.3 | Error Suggestion | AA | Not verified | — | — |
| 3.3.4 | Error Prevention (Legal, Financial, Data) | AA | Inspected | Destructive actions confirm, with a typed tier for the worst (`web/src/ui/ConfirmDialog.tsx`); archive is undoable — but see 2.2.1. | — |
| 3.3.7 | Redundant Entry | A | Inspected | No flow found that asks again for something already entered. | — |
| 3.3.8 | Accessible Authentication (Minimum) | AA | Inspected | Identity is the tailnet's, or a passkey on an exposed install (`web/src/features/auth/LoginPage.tsx`): no password, CAPTCHA or code to remember or transcribe. Adding an account shows a device code as selectable text with no copy button (`web/src/features/accounts/DeviceCodePanel.tsx`); the entry happens on the provider's site. Left: the passkey prompt with a screen reader. | needed |

### Principle 4 — Robust

| SC | Name | Level | Status | Evidence, and what is left | AT |
|---|---|---|---|---|---|
| 4.1.2 | Name, Role, Value | A | Partly automated | Matrix gate and the `jest-axe` suites (`web/src/features/sessions/list/a11y.test.tsx`). Deviations L2–L4 in the limitations doc. What a screen reader actually says is the open half. | needed |
| 4.1.3 | Status Messages | AA | Inspected | Live regions exist where status changes: `web/src/ui/PageContent.tsx`, `web/src/ui/Banner.tsx`, `web/src/features/sessions/SessionAriaLive.tsx` (new assistant output, hand-backs). Whether they are spoken, and not spoken twice, is unknown. | needed |

(4.1.1 Parsing was removed in WCAG 2.2.)

### Totals

55 criteria: 15 automated, 9 partly automated, 15 inspected, 0 with a known gap, 9 not verified, 7 not applicable.
28 need assistive technology or a device to confirm; 0 of those are done.

## 3. Assistive technology and device verification — not yet run

This needs a person with the hardware. It cannot be done by an agent, by emulation, or by reading
the accessibility tree: the point is what the combination of a real screen reader and a real
browser does with the page.

### Combinations

| Id | Assistive technology | Browser | Platform | Note |
|---|---|---|---|---|
| VO-iOS | VoiceOver | Safari | iPhone | The primary phone path. |
| NVDA | NVDA | Firefox or Chrome | Windows | A Windows VM is available on a session runner — `docs/runbooks/windows-vm-on-a-session-runner.md`. |
| TB | TalkBack | Chrome | Android phone | |
| HC | Contrast theme (forced colours) | Edge or Chrome | Windows | Same VM. No screen reader needed. |

### Journeys

Run each journey in each combination. Use a build with seeded or throwaway data — never record a
real prompt, account name or transcript in the results.

| # | Journey | What to listen and look for |
|---|---|---|
| J1 | Open the app; move between Sessions, Pipeline, Roadmap, Delivery, Settings. | Each page announces its title and one heading. Landmarks are listed and named. The current section is identifiable. |
| J2 | Start a session: choose a repo, type a prompt, Create. | Every control has a name. The new session is announced. Focus lands somewhere sensible. |
| J3 | Open a working session; wait for output; answer a permission request. | New output is announced once, not per token and not twice. The permission request interrupts. The answer buttons are reachable and named. |
| J4 | A session hands back with a question; answer it. | The hand-back is announced. The question and its options read in order. |
| J5 | Archive a session, then undo. | The toast is announced, stays until dismissed, and Undo can be reached. |
| J6 | Settings: change a value, trigger a validation error, fix it, save. | The error is announced and tied to its field. Save's result is announced. |
| J7 | Open and close a dialog and a menu. | Focus moves in, is trapped in the dialog, and returns to the trigger. Escape closes. |
| J8 | Lose the connection (airplane mode), then restore it. | The outage and the recovery are both announced. Nothing is lost from the composer. |

### Device checks (no screen reader)

| # | Check | How | Pass when |
|---|---|---|---|
| D1 | Rotation | Rotate mid-task on J2 and J3, both phones. | Nothing is lost; the composer keeps its text and focus. |
| D2 | Software keyboard | Focus the composer and each Settings field. | The focused field and the page header stay visible above the keyboard. |
| D3 | Safe areas | Notched iPhone, portrait and both landscapes. | Nothing sits under the notch, the home indicator or the rounded corners. |
| D4 | Large text | iOS Larger Text and Android font size at maximum; desktop browser at 200%. | No clipped or overlapping text; every control still reachable. |
| D5 | 400% zoom | Desktop browser, 1280×1024 window, 400%. | One-direction scrolling only. |
| D6 | Reduced motion | The OS setting, on each platform. | No pulse, shimmer or slide. |
| D7 | Forced colours | Windows contrast themes, light and dark. | Focus is always visible; the current section and selected tab are distinguishable; no control disappears. |
| D8 | Hardware keyboard | Bluetooth keyboard on iPhone and Android. | J1–J7 complete without touching the screen. |
| D9 | Switch or voice control | iOS Switch Control or Voice Control, one pass of J2. | Every control can be targeted by name or by scanning. |

### Results

Record one row per combination × journey or check. A failure gets an issue and its row in §2 changes.

| Date | Tester | Combination (with versions) | Journey / check | Result | Issue |
|---|---|---|---|---|---|
| — | — | — | — | not run | — |

## 4. What this audit changed

- **Forced-colours focus** (2.4.7). Every focus ring is a box-shadow beside `outline-none`; forced
  colours removes shadows, so nothing marked focus — measured 0 of 81 tab stops. One unlayered
  rule in `web/src/index.css` restores an outline for every control, present and future, and marks
  the current page and selected tab.
- **The seeded backend rendered two error pages.** `/pipeline` and `/roadmap` threw on the seed's
  default response, so the #1412 matrix had been scanning "This page hit a problem" for those
  routes, and every route wore a false "Can't reach the session daemon" banner. Fixed in
  `web/e2e/fixtures/seed.ts`; settling now refuses an error page (`web/e2e/support/routes.ts`).
- **Pipeline health lanes** (1.4.3, 2.5.3), hidden until the seed rendered the page: unwired lanes
  were dimmed with opacity to 2.4–4.3:1, and each lane's `aria-label` replaced its visible title
  (`web/src/pipeline/HealthHeader.tsx`). **Daemon-health pill** (4.1.2): its subject was an
  `aria-label` a role-less span may not carry (`web/src/shell/DaemonHealthIndicator.tsx`).
- **The adaptation gate** — reflow, zoom, text size, text spacing, reduced motion, focus, forced
  colours — with seeded defects proving each check can fail.

Four gaps were found and filed — #3561 (1.4.10), #3562 (2.1.4), #3563 (2.2.1), #3564 (2.5.7).
All four were fixed while this was in review; their rows above name the test that now holds each.
