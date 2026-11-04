# Desktop-parity D-15: the PR tab — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** add a **PR** tab to the session detail view. It shows the session's pull request with:
- its checks;
- its review threads (file, line, resolved state and comments);
- its merge state;
- these actions: Ready + auto-merge, Disable auto-merge, Merge now, Fix, and Open on GitHub.

All of it is built from the Pipeline data ViberPit already polls, plus the one live per-PR detail call.

**Decision:** D19, "Adopt all: … a PR tab (checks, threads, merge/auto-merge, from Pipeline data)".

**Spec:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §2 (Tabs) and §11 D-15.

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

## Facts (mapped 2026-11-04; re-verify the line numbers)

- **Session tabs.**
  - `SESSION_VIEWS` lives in `web/src/features/sessions/sessionView.ts:17` and is selected by `?view=`.
  - Listing, panels and lazy imports are all in `web/src/features/sessions/SessionBody.tsx`: the `listed` map at ~`:112`, items at ~`:214`, panels at ~`:381`.
  - `web/src/ui/Tabs.tsx` is the only tablist; `tablistSingleSource.test.ts` enforces that.
- **Session PRs.**
  - `Session.prs` (`web/src/api/types.ts:423`) comes from `orch.prs_for()`, which reads the per-branch poller cache.
  - The server already sends `is_draft`, `head_ref`, `head_sha`, `checks_detail`, `mergeable`, `merge_state_status`, `auto_merge_enabled`, `in_merge_queue` and `labels` (`server/github/pr_status.py:166-205`), but the TS `PullRequestInfo` doesn't declare them.
- **Detail.**
  - `GET /api/prs/{owner}/{name}/{number}` → `orch.pr_detail` (`orchestrator.py:~20620`) merges the population cache with a live GraphQL call (`build_pr_detail_query` / `parse_pr_detail_response`, `pr_status.py:~841-890`).
  - It returns checks with urls, `reviews` (author and state only), and `mergeable`.
  - It 404s when the PR isn't in the population cache, and no web hook calls it.
- **Review threads with bodies are not fetched anywhere.** The only `reviewThreads` query (`server/sessions/dispatch.py:35`) just counts unresolved threads.
- **Mutations.**
  - `POST /api/pipeline/pr/ready-merge` → `orch.ready_merge` (`orchestrator.py:~20593`) runs `gh pr ready` and then `gh pr merge --squash --auto`. It has **no `hold:human` check**.
  - Nothing exists to merge now or to disable auto-merge.
  - Fix is `POST /api/sessions/{id}/fix-pr {number}` (hook `useFixPr`).
- **Holds.** `has_human_hold` / `HUMAN_HOLD_LABEL` are at `pr_status.py:24-30`. The Bash-hook guards (`merge_hold_guard.py`) cover a session's own shell only, not REST.

## Global Constraints

- **One data path.**
  - The tab reads the session's PR list from the session payload (`Session.prs`) and the selected PR's detail from `GET /api/prs/{o}/{n}/{num}`.
  - Don't add a second PR-status poller or cache.
  - When the population cache misses, `pr_detail` falls back to the session's per-branch cache entry (`prs_for`, searched by repo + number) before returning 404, so a session PR on a repo outside the Pipeline population still opens. Add this fallback inside `pr_detail`, not in the route.
- **Review threads.**
  - Extend `build_pr_detail_query` / `parse_pr_detail_response` with `reviewThreads(first: 50)`. Fetch `id isResolved isOutdated path line originalLine` and `comments(first: 20){ author{login} body createdAt url }`.
  - Return `threads: [{id, path, line, resolved, outdated, comments:[{author, body, createdAt, url}]}]` in API order.
  - Bodies are markdown text. The web renders them with the existing markdown renderer the conversation uses, which is sanitized; never use raw HTML.
  - Parsing fails open: a malformed thread is dropped and the rest still returns, the same as the parser's existing behaviour.
  - Also return `isDraft`, `autoMergeEnabled`, `mergeStateStatus`, `labels` and `humanHold` on the detail, from the cached entry.
- **Mutations.** Each runs through `orch` with `gh` and the app token, the same way `ready_merge` does.
  - `POST /api/prs/{o}/{n}/{num}/merge {method: "squash"}` → `gh pr merge N -R repo --squash`. Only squash is accepted; anything else is 422.
  - `POST /api/prs/{o}/{n}/{num}/auto-merge {enabled: false}` → `gh pr merge N -R repo --disable-auto`.
    - `enabled: true` reuses `ready_merge`; don't add a second arm path.
    - The existing `POST /api/pipeline/pr/ready-merge` stays as it is for the Pipeline page.
  - Each mutation returns `{ok, error}` with gh's stderr verbatim on failure, as `ready_merge` does. Logs are INFO, never WARNING.
- **Human hold (security).**
  - `hold:human` means a human owns that PR's merge. Merge-now, arming auto-merge, and `ready_merge` itself (the Pipeline route included) must refuse a held PR with 409 `PR_HUMAN_HOLD`. Read the labels from a **live** fetch, not only the cache, so a just-added hold counts.
  - Disable auto-merge is always allowed, because it is the safe direction.
  - The UI shows `Held for a human — merge it on GitHub` and hides merge and arm.
  - Never remove the label.
- **Audit.** `pr.merge`, `pr.auto_merge_disabled` and `pr.ready_merge` audit rows record identity, repo and number.
- **Assistant policy.** Do not add the new mutation routes to the assistant-token allowlist (`server/assistant/policy.py`). Add a test pinning that they are absent.
- **Remote mode.** These are edge-side `gh` calls with no per-session RPC; `pr_detail` / `ready_merge` already run on the edge orchestrator. Verify the routes work with the remote/fleet facade, as `pr_detail` does today (`api-orchestrator-seams-do-not-exist-on-remote-orchestrator` memory: no private seams).
- **Web.**
  - **Placement.**
    - Add `'pr'` to `SESSION_VIEWS`, labelled **PR**, placed after Changes.
    - It is listed only when `session.prs.length > 0`.
    - With several PRs, a compact picker (a segmented control or select, `#123 title`) chooses one. The default is the lowest-numbered open or draft PR, otherwise the newest.
    - The panel is lazy, like `ChangesTab`.
  - **Header.** Title, `#N`, a state badge (Open / Draft / Merged / Closed), Auto-merge on, In merge queue, Held for a human, and the merge state in words (`CLEAN` → "Ready to merge", `BLOCKED` → "Blocked by required checks or reviews", `BEHIND` → "Behind base", `DIRTY` → "Has conflicts", `UNSTABLE` → "Non-required checks failing", `DRAFT`, unknown → as-is), then Open on GitHub.
  - **Checks.** Grouped as failing / pending / passing, with failing expanded. Each row shows name, conclusion and a link. Reuse `CheckGantt` only if it renders cleanly at 390px; otherwise use a plain list. Don't fork it.
  - **Threads.**
    - Unresolved threads first, each with `path:line` (link to the Files tab via the existing `useFileTarget` `?view=files&file=&line=` when the path exists in the worktree), its comments, and an **Outdated** chip.
    - Resolved threads are collapsed under "N resolved".
    - Each unresolved thread has **Ask session to address**, which prefills the session composer with `Address this review comment on <path>:<line>:\n\n> <first comment body, quoted>` and switches to the Conversation tab.
      - Find the existing composer-prefill seam (grep for how templates or quotes insert text) and reuse it.
      - Don't send it automatically.
  - **Actions.** Show only what applies.
    - **Ready + auto-merge** for a draft, or an open PR without auto-merge. Reuse `readyMergeGate` from `web/src/pipeline/PrRow.tsx`; don't duplicate it.
    - **Disable auto-merge** when it is on.
    - **Merge now** when `mergeStateStatus === 'CLEAN'` and the PR is not a draft. It asks for confirmation through the existing confirm dialog.
    - **Fix** when checks are failing and the PR is open or draft. Use `useFixPr`, the same predicate the card uses (`anyChecksFailing` / `isActionablePrState`).
    - All of them are hidden when `humanHold` is set, except Disable auto-merge and Open on GitHub.
  - **Data.**
    - Add `usePrDetail(repo, number)` (React Query), polling every 30 s only while the tab is visible.
    - Add `useMergePr` and `useSetAutoMerge`.
    - All of them invalidate the detail and sessions queries on success.
    - Declare the missing `PullRequestInfo` fields in `types.ts`.
    - Update every manual `@/api/hooks` mock that the new hooks would break; run the full vitest.
  - **Errors.** A 404 or 502 from detail shows the payload-level data (title, state and checks from `Session.prs`) with `Live details unavailable — <reason>`. A mutation error shows gh's text inline.
  - **Layout.** Mobile-first at 390px and 1440px. Use the house tokens and components (`web/src/ui/*`); no new design tokens.
- **Gates.**
  - Server: ruff, mypy `server/`, focused pytest, plus the full `server/tests/ -n 4` before the PR.
  - Web: typecheck, lint, full vitest, build, budget (ceilings are in `web/bundle-budget.json`; the tab is lazy, so the entry should not move), and e2e.

## Review Focus

1. **A PR labelled `hold:human` in GitHub but not yet in the cache:** merge-now and arm refuse with `PR_HUMAN_HOLD`, from the live fetch. Pinned in Task 1.
2. **The Pipeline `ready-merge` route on a held PR:** now refused, and the Pipeline UI shows the error text. Pinned in Task 1, with a web check in Task 2.
3. **A thread body containing `<script>` or a markdown link with a `javascript:` href:** rendered inert. Pinned in Task 2.
4. **A session whose PR is outside the population cache:** detail still loads through the `prs_for` fallback. Pinned in Task 1.
5. **A PR merged while the tab is open:** the poll shows Merged and every action except Open on GitHub disappears. Pinned in Task 2.

---

### Task 1: Server — threads in PR detail, merge / disable-auto, hold guard

**Files:**
- `server/github/pr_status.py` (query and parser).
- `server/sessions/orchestrator.py`: `pr_detail` with the fallback and the extra fields, `merge_pr`, `disable_auto_merge`, `ready_merge` with the hold check, and a live-label helper.
- Facade and remote passthroughs as needed (the edge only).
- `server/api/pipeline.py`: routes and audit.
- Tests: `server/tests/test_pr_tab.py`, plus an assistant-policy absence test.

- [ ] **Step 1: Failing tests**
  - The parser returns threads with `path`, `line`, `resolved`, `outdated` and comments, and drops malformed ones.
  - Detail returns the threads and the new fields.
  - The population miss falls back to `prs_for` (Review Focus 4).
  - Merge runs `gh pr merge --squash`; a non-squash method gets 422.
  - Disable runs `--disable-auto`.
  - A held PR (by the live label) gets 409 from merge, arm and ready-merge, while disable still passes (Review Focus 1 and 2).
  - Audit rows are written, logs are INFO, and the routes are absent from the assistant allowlist.
- [ ] **Step 2–4.** Implement, then run the focused tests, ruff and mypy.
- [ ] **Step 5: Commit** `feat(pipeline): review threads in PR detail, merge / disable auto-merge, hold:human guard (D-15)`

### Task 2: Web — the PR tab

**Files:**
- `sessionView.ts` and `SessionBody.tsx` (listing, item, lazy panel).
- New `web/src/features/sessions/pr/PrTab.tsx` and any small subcomponents.
- `api/hooks.ts` (`usePrDetail`, `useMergePr`, `useSetAutoMerge`) and `types.ts`.
- The hook mocks.
- Tests: unit tests, plus e2e `web/e2e/pr-tab.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1: Failing tests**
  - The tab is listed only with PRs, and `?view=pr` selects it.
  - The picker defaults correctly.
  - The header badges and the merge-state copy are right.
  - Checks are grouped.
  - Threads are ordered with unresolved first and resolved collapsed. The Files link appears and "Ask session to address" prefills without sending.
  - Each action shows and hides by state, posts the right body, and shows the error text.
  - A hold hides merge and arm.
  - Merged state hides the actions (Review Focus 5).
  - XSS bodies are inert (Review Focus 3).
  - Detail failure falls back to payload data.
- [ ] **Step 2–4.** Implement, then run the full web gates, build, budget and e2e.
- [ ] **Step 5: Commit** `feat(web): PR tab — checks, review threads, merge and auto-merge (D-15)`

### Task 3: Benchmark and docs

- **Benchmark.** Set the row `PR viewer: checks, threads, merge, auto-merge` → Y ("PR tab"; merge queue is shown read-only), regenerate audit §3, and report the target (D-14: 91%).
- **Spec.** Record the tab's data path, the hold guard on REST merges, and "Ask session to address" in §2 and §11.
- **Product docs.** Cover the session PR tab.
- **Ceilings.** Measured + 0.3, only if they moved.
- [ ] Commit `docs: D-15 benchmark rerun and PR tab docs`.
