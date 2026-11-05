# Release Milestones Phase 4 (Export / Print to PDF) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "Export PDF" button to the Milestones page. It prints the live view as one clean landscape page, and nothing is saved or synced anywhere.

**Architecture:** A `@media print` stylesheet scoped to the milestones page, plus `window.print()` (spec M6 and §9). There is no server-side rendering. The print layout reuses the live React tree and hides everything interactive.

**Spec:** `docs/superpowers/specs/2026-11-05-release-milestones-design.md`, M6 and §9 "Export".

## Global Constraints

- Paper: `@page { size: letter landscape; margin: 0.4in }`.
- The print must fit **one page** for the KinDash fixture and for any ladder up to 6 milestones. Content past the caps is truncated, never paginated.
- Hide everything that isn't content:
  - the app shell: nav rail, top bar, sidebar list, daemon banner;
  - the auto-refresh banner;
  - every button and dialog.
- Print in light colors on white, using the existing tokens' print-safe values. Status keeps its glyph and text, as on screen.
- Add a stamp: "Snapshot <local date-time> · <product name> · ViberPit". Format the local time with the browser's locale and time zone.
- Do not add new routes or server code. Keep all code inside `web/src/features/milestones/`, which is the lazy chunk; the bundle budget is at its ceiling, so nothing may go into the startup bundle.
- Grep `web/src` for an existing print stylesheet convention before adding one. As of this plan there is none.

## Review Focus

1. **A ladder with long condition labels and many open leaves.** The print must still fit one page: labels clamp to 2 lines, each card shows at most 4 open leaves plus "+N more", and the Decisions list shows at most 6 plus "+N more".
2. **More than four milestones.** Cards shrink to fit one row of N, and the minimum card width is waived in print.
3. **Dark mode active when printing.** The print is still light on white.
4. **The browser's own header and footer.** The page must work without relying on them; the stamp carries the time.
5. **Printing while a dialog is open.** The dialog is hidden in print.

### Task 1: Print stylesheet, Export button, and a one-page test

**Files:**
- Create: `web/src/features/milestones/print.css`, imported only by `MilestonesPage.tsx`, so Vite places it in the lazy chunk. If a CSS module or a Tailwind `print:` variant fits the repo's conventions better, use that instead and say why.
- Create: `web/src/features/milestones/PrintStamp.tsx`, a print-only stamp.
- Modify: `MilestonesPage.tsx` (Export PDF button in the header strip, plus the stamp), `MilestoneCard.tsx`, `ConditionRow.tsx`, `NotesPanel.tsx`, `NumbersPanel.tsx`. Add `data-print=` hooks or classes only where the CSS needs them.
- Test: `web/src/features/milestones/MilestonesPage.print.test.tsx`, and `web/e2e/milestones-print.spec.ts`.

**Interfaces and decisions:**
- **Button:** labelled "Export PDF", calls `window.print()`. It sits beside Refresh and is hidden in print.
- **Print layout:**
  - Header: product name, ladder title, "You are here: …", and the gate summary.
  - The ladder in one row of N columns (`grid-template-columns: repeat(N, minmax(0, 1fr))`).
  - Below it, three columns: Needs you, Numbers, Decisions.
  - Font: base about 9pt, card titles 10.5pt, the header 13pt.
- **Caps in print only:**
  - Condition labels clamp to 2 lines (`-webkit-line-clamp`).
  - At most 4 open leaves per card, then "+N more".
  - Done leaves stay as a count.
  - At most 6 decisions and 6 asks, each followed by "+N more".
  - Render the "+N more" text in the DOM, marked print-only, so both tests can see it.
- **Breaks:** `break-inside: avoid` on cards. Set `html, body { height: auto; overflow: visible }` in print, so the app's inner scroll pane doesn't clip the content.
- **Links:** print as text. Don't append the URL.

- [ ] **Step 1: Write the failing tests.**
  - Unit tests:
    - `export button calls window.print`
    - `print stamp renders product and a local timestamp`
    - `print caps render +N more for leaves, decisions and asks` (render the fixture with 9 open leaves, 8 decisions and 8 asks)
  - e2e test `milestones-print.spec.ts`: mock GET with the fixture payload, extended with notes and many leaves. Then:
    - call `page.emulateMedia({ media: 'print' })`;
    - call `page.pdf({ format: 'Letter', landscape: true, printBackground: true })`;
    - assert the page count is 1 by counting `/Type\s*\/Page[^s]` in the PDF bytes;
    - repeat with a 6-milestone payload and assert 1 page;
    - save the KinDash-fixture PDF to `test-results/milestones-print.pdf` for the visual check.
- [ ] **Step 2:** Run the tests. They fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. They pass. Then run the full web gate (typecheck, lint, test, build, budget) and `npx playwright test e2e/milestones-print.spec.ts e2e/milestones.spec.ts`.
- [ ] **Step 5: Visual check.** Render the e2e PDF's first page to PNG (`pdftoppm -png -r 80 -f 1 -l 1` if available, otherwise a Playwright print-media screenshot at 1056×816). Open it and confirm it is readable at a glance and fits one page. Save it to the scratch dir.
- [ ] **Step 6: Commit:** `feat(web): one-page landscape Export PDF for the milestones view`
