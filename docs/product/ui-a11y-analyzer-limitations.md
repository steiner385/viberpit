# UI accessibility analyzer — classified limitations

Status: current as of 2026-11-26 (axe-core 4.14.1, nested under `@axe-core/playwright` 4.14.1). Owner: the
cross-browser gate, #1412 (G14, part of #1398).

The cross-browser E2E gate (`web/e2e/cross-browser/`) runs an axe scan in the engine under
test for every route × layout mode. **There is no blanket `disableRules`.** A finding is
either fixed, or it is classified here and in `ANALYZER_LIMITATIONS`
(`web/e2e/support/a11y.ts`) — the table in code is what the gate reads; this page explains it
to a person. A finding in neither place fails the run. That includes axe's *incomplete*
("needs review") bucket, because "incomplete" is exactly where an analyzer hides what it could
not decide.

Each entry says what covers the ground instead. If you add an entry, say that too, or fix the
finding.

## Classification

| Kind | Meaning |
|---|---|
| `analyzer-false-positive` | The analyzer reports something that is not a defect in our markup. |
| `analyzer-undecidable` | The analyzer cannot decide; a different control decides it. |
| `known-deviation` | A real, accepted, tracked deviation. Not silenced — just not blocking, and listed so it is not forgotten. |

## Active entries

| Id | Rule (bucket) | Kind | Why | Covered by |
|---|---|---|---|---|
| L1 `contrast-overlap` | `color-contrast` (incomplete) | undecidable | axe cannot compute a background for text under another positioned element, so it reports "needs review" rather than a ratio. | `web/src/__tests__/contrast.test.ts` asserts every foreground/background **token** pair in both themes. axe still fails on any node it *can* measure. |
| L2 `session-listbox-label` | `aria-prohibited-attr` (incomplete) | known deviation | The session list is a deliberately role-less `<div aria-label>`: the dock textarea `aria-controls` it and owns `aria-activedescendant`, which is not allowed on a role-less element. | `docs/ACCESSIBILITY.md` §2; the list is also reachable as the labelled `<aside>` that contains it. |
| L3 `status-pill-label` | `aria-prohibited-attr` (incomplete) | false positive | `StatusPill` puts `aria-label` on a `<span>` **and** renders the same words as visible text, so nothing depends on the prohibited attribute being honoured. | `ui/StatusPill.test.tsx` (icon + text, never colour alone). |
| L4 `usage-badge-label` | `aria-prohibited-attr` (incomplete) | known deviation | The session-card usage badge puts a descriptive `aria-label` ("weekly usage: 50% — no active window") on a role-less `<span>`. Unlike L3, the label adds words the visible "50%" does not carry. | Open follow-up: give the badge `role="img"` or move the words into visually-hidden text. Tracked in the conformance scorecard. |
| L5 `logotype-contrast` | `color-contrast` (violation) | false positive | The "Pit" half of the wordmark is the brand green at 2.3:1. WCAG 1.4.3 exempts text that is part of a logo or brand name; axe cannot know a span is a logotype. It is `aria-hidden`; the accessible name is the whole word. | `shell/BrandMark.tsx` documents the exemption; `--t-brand` is used for the logotype only. |

## The two findings the audit named

The 2026-09 UI audit reported "invalid modern-color and native-checkbox-switch findings" from
its analyzer output. Both were checked against the tool the gate actually runs, in all engines
the gate runs, rather than carried forward.

**Modern colour (`oklch()`, `color-mix()`).** Not a limitation of axe 4.14: it evaluates both.
`seeded-defects.spec.ts` writes low-contrast text three ways (`oklch()`, `color-mix()`, plain
hex as a control) and asserts that axe reports each as a *violation* — not as "incomplete" —
on every engine. If a future axe stopped parsing a colour function, contrast would silently
move from "failing" to "needs review", and that test is what would notice. (The stylesheet
itself authors its tokens as hex; Tailwind v4's own palette is `oklch`.)

**Native checkbox switch.** `PluginsSection` ships `<input type="checkbox" role="switch">`.
axe accepts the pairing (`aria-allowed-role` does not fire), and still fails the construct when
its name is missing — both asserted in `seeded-defects.spec.ts`. So the finding is not
reproducible with this analyzer. (`ui/Switch`, a `<button role="switch">`, is the shared
switch primitive; `PluginsSection` and `McpServerList` each hand-roll their own instead of
using it — a consolidation candidate, listed in the scorecard, not an accessibility defect.)

**Not reproduced.** The audit's own analyzer output was a local file
(`~/Documents/ViberPit-UI-Review-2026-09-07/report.html`) that automation cannot read, and the
design-detector it most likely came from (Impeccable) is not approved to run
(`docs/engineering/frameworks/impeccable-admission.md`). Rather than run it, the two concerns
were validated directly as above. If the owner can name the tool and the exact finding text, add
a regression for it next to these.

## Real defects the gate found and this change fixed

Not limitations — fixed, with a regression each (`shell-modes.spec.ts` › "Confirmed gaps").

- Session-card **Quick replies** chevron was 20px wide on a hover-capable pointer, under the
  24px of WCAG 2.2 SC 2.5.8. Now 24px.
- **Settings › Plugins** and **Settings › MCP** dimmed text with `opacity-50/60/70`, which
  measures 3.2–4.3:1 against the 4.5:1 floor. Now the `ink-muted` token.
- The phone dispatch bar's **More options** button used `aria-label`, which replaced its
  content — so the "non-default option selected" hint inside it was never announced (WCAG
  2.5.3). The name now comes from content.

## Adding or removing an entry

1. Reproduce it with the gate, not by reading axe docs: run `npx playwright test
   e2e/cross-browser` and read the failure.
2. Prefer fixing. If you cannot, add the entry to `ANALYZER_LIMITATIONS` **narrowly** — a
   `targetIncludes` that matches the one node, never a whole rule — and add the row above.
3. Delete an entry the moment the underlying cause is fixed; a stale entry is a blind spot.
