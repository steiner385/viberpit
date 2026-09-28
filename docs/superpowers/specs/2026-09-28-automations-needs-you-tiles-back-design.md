# Automations detail: Needs you tab, stat tiles, phone Back, status labels

**Date:** 2026-09-28 · **Status:** approved design · **Builds on:** the 2026-09-27 audit
(`2026-09-27-automations-pages-layout-audit.md`, "Recommendations not taken") and the page-tabs
standard (`docs/PAGE-TABS.md`). Four bounded pieces on the Delivery → Automations detail pane, one
of which also touches Roadmap because the two share the list/detail chassis.

## 1. Needs you tab

**Problem.** Every automation's actionable items (Error Triage: 50 muteable auto-errors; the runner:
19 implementable issues) render on Overview in a 192px scroll box — a list longer than three rows
where the standard forbids one (§5), with no tab of its own.

**Design.**
- `AUTOMATION_TAB` gains `'needs-you'` after `overview`, label `TIER_LABEL['needs-you']`
  ("Needs you" — the §3 word, already shared with Roadmap). Order: Overview · Needs you · Queue ·
  Schedule · Config · Events · Sessions.
- Offered to **every** automation: the question "what needs a person?" exists for all of them (§4:
  never hide a tab because it is empty right now). Badge `count = automation.actionableCount ?? 0`
  from the list payload (no extra fetch for the badge), `attention` when above zero.
- The tab renders the full actionable list on the page scroll: no inner scroll box, no heading.
  Empty: "Nothing needs you." `AutomationActions` gets a `limit?: number` prop; the tab passes none.
- Overview keeps a **preview**: `AutomationActions` with `limit={3}` under the "Actionable items"
  section heading, followed by a ghost button "See all N" (N = the list's length) that calls
  `setTab('needs-you')` when the list is longer than three. Still renders nothing at zero (§5
  notice shape).
- **Seven tabs** on the Backlog Runner and PR Doctor (the two with a Queue). Recorded in
  `docs/PAGE-TABS.md` §8 as an exception: "Automation with a Queue — 7th tab past the cap; Needs you
  (decisions) and Queue (dispatchable work) are different questions, and Schedule stays separate
  while its editor saves as a form beside Config's live-patching fields. Re-review when the
  schedule editor patches live."
- `?tab=needs-you` is URL state like every other tab; `AutomationDetail`'s tab filter is unchanged
  except for the new id.

## 2. Stat tiles from `status.counts`

**Problem.** The runner's fleet counts ("14 eligible, 63 blocked, 39 untriaged, 66 needs-human")
exist only inside `status.text`, a string the header now clamps. The server already publishes
them as `status.counts` on every automation (`AutomationStatus.counts: Record<string, number> |
null`), unused by the web.

**Design.**
- `AutomationDetail` renders the roadmap `StatTiles` above the strip (§6) from `status.counts`:
  one tile per key, in the server's key order, capped at **six** tiles, label = key humanized
  (`needs_human` → "Needs human", `gh_projects` → "Gh projects"; a small `humanizeKey` in
  `automation/countTiles.ts` next to a `countTiles(counts)` helper that owns the cap and order).
  No tone, no click: the counts do not map to tabs, and a tile that navigates nowhere must not
  look like one that does. `counts` null/empty → no tile row.
- `StatTiles` moves from `features/roadmap/detail/` to `ui/StatTiles.tsx` (it is now shared by two
  features; roadmap imports update). Its grid stays 2/4 columns; six tiles wrap to two rows.
- **Server:** the runner's `status().text` drops the counts clause. Today:
  `"{n} work in flight, {eligible} eligible, {blocked} blocked, {untriaged} untriaged,
  {needs_human} needs-human · {product segments} · {pick}"`. After:
  `"{n} work in flight · {product segments} · {pick}"`, and when `"eligible"` is absent from
  `counts`: `"{n} work in flight · queue counts unavailable"`. The `unattributed` segment, the
  admission/error branches and the idle sentence are unchanged. Tests that pin the old text
  (`test_backlog_runner_automation.py:229`, the "in" assertions in `test_automations_api.py`,
  `test_backlog_runner_e2e.py`, `test_backlog_runner_loop.py`) are updated to the new shape.

## 3. Phone/tablet Back

**Problem.** Below desktop the list column hides behind an open item (`app.list` matrix), so the
only way back to the list is the 12px breadcrumb.

**Design.**
- `DeliveryPage` and `RoadmapPage` pass `PageHeader.back` **only when `useViewport().mode !==
  'desktop'`** and an item is open: Delivery → `{ to: deliveryPath(scope), label: 'Back to
  <Automations|CI health>' }`; Roadmap → `{ to: roadmapPath(scope), label: 'Back to <Map|…>' }`
  (the scope the open item belongs to, so Back lands on the list that contained it). Desktop keeps
  the list beside the item and shows no Back.
- Both sections are `showInKiosk: false`, so `PageHeader`'s "never pass `back` from a kiosk page"
  rule holds. `InitiativePage` (a routed page with its own breadcrumb) is out of scope.
- The breadcrumb is unchanged.

## 4. Session status labels

**Problem.** The Sessions tab prints API keys (`waiting_external`, `pending_approval`).

**Design.** `SessionsList` renders `statusPresentation(status).label` from
`web/src/session/statusTier.ts` ("Waiting…", "Awaiting approval") — the one existing map. The
"N active" line and the link targets are unchanged.

## Testing

- Unit (vitest): Needs you tab presence on every automation, badge/attention from
  `actionableCount`, full list in the tab, three-row preview + "See all N" on Overview, URL state
  `?tab=needs-you`; `countTiles` (order, cap at six, humanized labels, null → empty); tiles
  rendered above the strip; Back link present below desktop and absent on desktop for both
  pages; status labels in the Sessions tab. `web/e2e/delivery.spec.ts` gains a Needs you step.
- Server (pytest): the runner status text assertions updated; a test that `counts` still carries
  every key the text used to spell out.
- Gates: typecheck, lint (`--max-warnings 0`), full vitest, the four `lint-and-test` steps, and
  the Delivery e2e spec run locally (the `e2e` CI job is not a required check).
