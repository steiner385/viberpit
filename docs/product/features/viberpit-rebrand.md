---
slug: viberpit-rebrand
title: ViberPit Rebrand (ccswitch → ViberPit, outside-in, zero-downtime)
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md
    - docs/superpowers/plans/2026-09-06-viberpit-rebrand-phase-0-1.md
  code:
    - src/ccswitch/brand.py
    - web/src/brand.ts
    - web/src/components/BrandMark.tsx
    - web/public/brand/
    - web/scripts/gen-icons.sh
    - server/hostpaths.py
    - server/auth/middleware.py
    - server/sessions/export.py
    - src/ccswitch/worktrees.py
    - server/sessions/orchestrator.py
    - server/deploy/steps.py
    - server/ipc/drain_on_signal.py
    - web/src/pipeline/notifyPref.ts
    - web/src/lib/repoFromCwd.ts
---

# ViberPit Rebrand (ccswitch → ViberPit, outside-in, zero-downtime)

**Problem** — The product shipped for months as `ccswitch` / `cc-account-switcher`, and that
name is baked into far more than a wordmark: it is the MCP tool prefix in five copies, the auth
cookie, the `X-CCSwitch-*` headers, 239 distinct `CCSWITCH_*` env vars, 12 systemd units that the
deploy controller restarts *by literal name* (including the unit it runs under), the `.ccswitch/`
namespace inside every user repo (holding live worktrees with absolute `gitdir:` pointers), the
`ccswitch/` branch prefix on remotes, PM `dedupe_key` rows, and the markers stored in every
transcript. Renaming to **ViberPit** in one PR would log every user out, strand every live
worktree, re-file every work item as new, and issue a daemon restart against a unit name that no
longer exists. The rename has to be a *series* of independently shippable phases, each of which
leaves the fleet working, ordered outside-in: external surfaces first, wire contracts with
dual-accept next, hosts and ops third, the per-repo namespace fourth, internal identifiers fifth,
living docs last.

**Target users** — everyone who sees the product name (dashboard users, operators reading unit
names and runbooks, GitHub readers of Backlog Runner comments) and every *program* that carries a
brand string across a process boundary: the edge, the session daemon, remote fleet workers on
rocky, the deploy controller, coding sessions that emit markers or call `mcp__viberpit__*` tools,
and the agent roles dispatched by name from the subagent-driven-development skill.

**Desired outcome** — `ViberPit` / `viberpit` is the only brand a person or program encounters on a
freshly provisioned host, while every legacy `ccswitch` spelling that was ever persisted or emitted
is still read correctly. Concretely:

| Surface | New value | Legacy handling |
|---|---|---|
| Display name, GitHub-posted text | `ViberPit` | none needed (done — #1358, #1362) |
| Env prefix | `VIBERPIT_*` | mirrored `CCSWITCH_*` at every entrypoint (done — #1382) |
| Markers, MCP server name / tool prefix | `<!-- viberpit:… -->`, `mcp__viberpit__` | **permanent** dual-accept — transcripts are re-parsed on replay (done — #1390) |
| Cookie, headers, localStorage, export format id | `viberpit_session`, `X-ViberPit-*`, migrate-on-read keys, `viberpit.normalized-events/v1` | read both for one release (#1368) |
| Branch prefix, PM `dedupe_key` | `viberpit/<base>-<id>`, `viberpit:*` | branch match `^(ccswitch\|viberpit)/` is **permanent**; dedupe keys get one `UPDATE`, no alias (#1369) |
| systemd units, host paths | `viberpit-*.service`, `/run/viberpit`, `/var/lib/viberpit` | runtime resolver lands first (#1370), then the rename with `Alias=ccswitch-*` for one release (#1371) |
| Per-repo namespace | `<repo>/.viberpit/` | new clones only; a repo with only `.ccswitch/` keeps it forever, nothing is moved (#1373) |
| Python package, console script | `viberpit`, `viberpit` | meta-path import shim `ccswitch.*` → `viberpit.*` and a `ccswitch` alias script for one release (#1374) |
| Web internals, eslint rule namespace, agent role names | `viberpit/no-raw-palette`, `viberpit-{implementer,reviewer}` | none needed — rename atomically per PR (#1375, #1376) |
| Living docs | README, `core.md` → `AGENTS.md`, `CLAUDE.md`, feature docs | historical docs under `docs/superpowers/**` and `specs/**` are never back-edited (#1377) |

Two constants own every name form — `src/ccswitch/brand.py` (`NAME`, `SLUG`, `LEGACY_SLUG`,
`ENV_PREFIX`, `MCP_TOOL_PREFIX`, …) and `web/src/brand.ts` — and `server/hostpaths.py` derives the
`/etc`, `/run`, `/var/lib`, `/var/log` stems from them with a prefer-existing legacy fallback. No
other module spells the brand; each later phase flips one line, not five.

**Success metrics** —
- Zero user-visible auth loss on the cookie flip: a request carrying *only* the legacy cookie and
  one carrying *only* the new cookie both authenticate (#1368 exit criteria).
- Zero stranded sessions: no live worktree is moved, and every `Session.cwd` row under a
  `.ccswitch/` segment still passes the orchestrator's `permanent_delete` guard after Phase 4.
- Zero duplicate work items: after the `dedupe_key` migration the PM sync re-files nothing.
- The unit-name resolver answers correctly on all three host layouts (legacy only, new only,
  both) before any unit file is renamed; the deploy controller never restarts a name that does not
  exist.
- An archive exported before the format-id flip imports cleanly after it.
- Every phase's grep gate covers `web/e2e` as well as `web/src` (the Phase 1 regression: #1362
  went green on unit tests and red on the Playwright job).
- Brand drift-guard tests keep failing any new hard-coded `ccswitch` / `ViberPit` literal outside
  the two brand modules.

**Non-goals** —
- Renaming things that are not the brand: `~/.claude-accounts` (already brand-free, sshfs-shared
  with rocky), `ccrun` / `ccmax` ("cc" is Claude Code, the engine), the `ccas1:` / `ccas2:`
  assistant token prefixes (a wire format held by external clients), the mTLS CA CN, the unix user
  `ccswitch-auth`, and the Postgres role/db name.
- Back-editing historical records under `docs/superpowers/{plans,specs,spikes}/` and `specs/`.
- Editing `.specify/memory/constitution.md` from an agent session — it is a `GUARDRAIL_PATHS` file
  whose diff needs an owner-written acknowledgment line; Phase 6 prepares the diff and hands it
  over.
- The Phase 3 maintenance window itself (#1372, `needs-human`): installing renamed units, moving
  `/etc/ccswitch/*.env` (uncommitted root state holding secrets), and the GitHub repo slug rename
  touch every live session and are never auto-claimed by the backlog runner.
- Removing any dual-accept reader that is documented as permanent (markers, MCP prefix, branch
  prefix) — they are not scaffolding.

**Notes** —
- **Dual-accept before emit is the standing rule.** Any string that crosses a process boundary or
  is persisted must be *accepted* in both spellings in a release that ships **before** the release
  that starts emitting the new one. The two-PR split of Phase 3 (#1370 resolver, then #1371
  rename) is the same rule applied to systemd: if the resolver and the rename land together, the
  restart that would activate the resolver is issued by code that has already forgotten the old
  name.
- **Persisted ≠ emitted.** Rows written before a flip outlive it, so the branch prefix and the
  `.ccswitch` path-segment guard are matched both ways forever, whereas `dedupe_key` gets a
  one-shot data migration because a dual key would re-file every work item. Nothing runs
  `alembic upgrade` at deploy and reconcile applies no data-only `UPDATE`, so the revision is the
  reviewable record and the script runs inside the Phase 3 window.
- **The import shim is load-bearing, not politeness.** The shared venv's editable `.pth` resolves
  `ccswitch.*` from the deploy checkout, so a worktree that has not been rebuilt would
  `ModuleNotFoundError` the moment the package renames without the meta-path redirect (#1374).
- **Agent role names rename in one PR** (#1376): dispatch is by name, and a role file renamed
  separately from the skill that dispatches it fails as a subagent that silently never starts, not
  as a test failure. `AGENTS.md` is generated (`agentctl render`, 320-line cap) and must be
  regenerated in the same change.
- **Remaining phases are sequenced, not parallel** — #1368 → #1369 → #1370 → #1371 → (#1372 window)
  → #1373 → #1374 → #1375 → #1376 → #1377. Phase 6 can only describe behaviour that has already
  shipped.

**Visual identity (2026-09-11)** — The owner supplied a mascot and wordmark, which supersedes the
design's assumed decision D3 ("keep the two-lane mark"). The two-lane SVG and its Chromium
renderer are gone; the brand is now three rasters under `web/public/brand/` and one script.

| Asset | File | Used by |
|---|---|---|
| Mascot (viper in sunglasses at a laptop, transparent) | `viberpit-mascot.png` (1024 px) | source of every icon |
| Horizontal logo (mascot + wordmark) | `viberpit-logo-horizontal.png` (1200 px) | `README.md` |
| Stacked logo | `viberpit-logo-stacked.png` (800 px) | docs, social |
| App icon / mark | derived: `favicon.png`, `apple-touch-icon.png`, `icons/icon-{192,512,512-maskable}.png` | tab, home screen, `BrandMark` |

- **One mark, cropped, not drawn.** `web/scripts/gen-icons.sh` crops the mascot's head onto a
  Deep Night plate with the head inside the centre 80% (Android's maskable safe zone) and the
  coil and laptop running off the edges, so the crop's cut lines fall under the rounded corners
  or the launcher's mask. Every icon target is a resize of that one plate; `BrandMark` in the UI
  is the same `icon-192.png`. Re-run the script after changing the mascot or the crop, and
  commit the PNGs — it is not part of `npm run build`.
- **Wordmark in text, not an image.** `Wordmark` renders "Viber" in ink and "Pit" in
  `text-brand` (`--t-brand`, Viper Green `#22c55e`), from `BRAND_NAME_PARTS` in `brand.ts`,
  which `brand.test.ts` pins to `BRAND_NAME`. The full name is one visually-hidden node so
  screen readers and `getByText` see "ViberPit" once. The phone `TopBar` and the desktop
  `NavRail` each show it; they never render together.
- **Brand colours are not UI tokens.** Viper Green (`#22c55e`) is 2.3:1 on white and Vibe Purple
  (`#8b5cf6`) is 3.4:1 — both fail AA for text, which WCAG 1.4.3 permits for a logotype and
  nothing else. `--t-brand` therefore exists for the wordmark only; `--t-accent` remains the
  interactive colour and the status tones are unchanged. The sheet's Deep Night (`#0b1020`) is
  the icon plate, not the dark canvas (`#16171d`, `shell/canvas.ts`), which stays as tuned.
- **Copy from the brand sheet** (owner-approved; typos in the mockup fixed here): tagline
  *Code it. Vibe it. Ship it.*; positioning *The open source cockpit for builders who'd rather
  create than click around.*; loop *Plan · Build · Test · Ship · Repeat*; alternates *Ideas to
  impact. On autopilot.* / *Your product lifecycle, less work.* / *Build more. Manage less.* /
  *From concept to commit.* / *Serious ideas. Less serious attitude.* / *Less grind. More build.*
- **Fonts stay system.** The sheet specifies Poppins / Inter / Caveat; the app keeps
  `--font-sans: system-ui` — a webfont for an eight-character wordmark is not worth a network
  fetch on a tailnet-only app, and the logo rasters already carry the display face.

**Legacy-brand ratchet (2026-09-12)** — Between phases the tree legitimately carries the old
spelling in places search-and-replace cannot touch, and the risk is that "later" quietly becomes
"never" while new mentions creep back in. `scripts/brand_ratchet.py` and its registry
`.brand-ratchet.toml` make the remainder a checked inventory, enforced by
`server/tests/test_brand_ratchet.py` inside the lint-and-test gate:

- Every `ccswitch` / `cc-account-switcher` spelling in tracked files (historical records under
  `docs/superpowers/`, `docs/audits/`, `docs/2026-*`, `specs/` and recorded fixtures excluded)
  must be **claimed** by one `[[allow]]` entry: a regex and/or path glob, plus either the GitHub
  `issue` that removes the category or a `permanent` reason citing the design decision that keeps
  it (the brand modules' `LEGACY_*` constants, the dual-accept parsers, the not-renamed table).
- **The rule for contributors:** a legacy spelling you cannot rename — a wire contract, a persisted
  key, host state, the Python package — needs an issue before it can stay. File it, add the entry,
  and the gate passes. An unclaimed spelling fails with the offending `file:line`.
- Each entry has a `ceiling`, the count when it was last tightened. A category that grows past it
  fails; one that shrinks passes and asks for `python scripts/brand_ratchet.py --update`, which
  rewrites only the ceilings, so the registry only ever ratchets down.
- `--list <id>` prints every hit an entry claims — the worklist for that entry's issue.

The 2026-09-12 sweep (this PR) took every unclaimed mention to zero: product-name prose in code
comments, docstrings and help text, living docs, `AGENTS.md` (via `core.md`), arbitrary test
fixtures, the CI Postgres names, the ESLint rule namespace (`viberpit/no-raw-palette`, the
web-internals half of #1375), and the `steiner385/ccswitch` slug (the repo is renamed). What
remains is claimed by #1368, #1369, #1373, #1374, #1376, #1377, the env sweep #1591 and the host
scaffolding removal #1592.

**Evidence** —
- **Epic and sub-issues:** #1365 (tracking; name-forms table, standing rules, out-of-scope
  window), #1368 (Phase 2b cookie/headers/localStorage/export id), #1369 (Phase 2c branch prefix +
  `dedupe_key`), #1370 (Phase 3a runtime unit/path resolver), #1371 (Phase 3b unit + ops-doc
  rename), #1373 (Phase 4 `.viberpit/` namespace resolver), #1374 (Phase 5a Python package +
  import shim), #1375 (Phase 5b web internal identifiers), #1376 (Phase 5c agent role names), #1377
  (Phase 6 living docs). Closed neighbours: #1366 (Phase 0b, PR #1382), #1367 (Phase 1c+2a, PR
  #1390); #1372 (Phase 3 window) stays `needs-human`.
- **Landed PRs:** #1349 (design spec), #1358 (Phase 0a brand source of truth + drift guards),
  #1362 (Phase 1a/1b display surfaces + GitHub-posted text), #1382 (Phase 0b env mirror), #1390
  (Phase 1c+2a marker/MCP dual-accept).
- **Design record:** `docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md` (§1 name forms
  and the deliberately-not-renamed table, §2 inventory, §3 phases, §4 risks) and
  `docs/superpowers/plans/2026-09-06-viberpit-rebrand-phase-0-1.md`.
- **Source of truth already in place:** `src/ccswitch/brand.py` (`NAME`, `SLUG`, `LEGACY_SLUG`,
  `ENV_PREFIX`/`LEGACY_ENV_PREFIX`, `MCP_SERVER_NAME`/`LEGACY_MCP_SERVER_NAME`, `CLI_NAME` pinned
  to the legacy slug until Phase 5), `web/src/brand.ts` (`MCP_TOOL_PREFIX`,
  `LEGACY_MCP_TOOL_PREFIX`), `server/hostpaths.py` (`RUN_DIR`/`STATE_DIR`/`LOG_DIR` via
  `prefer_existing`).
- **Surfaces still on the legacy spelling (the remaining phases' targets):**
  `server/auth/middleware.py` (`_COOKIE_NAME = "ccswitch_session"`),
  `server/sessions/export.py` (`NORMALIZED_EXPORT_FORMAT = "ccswitch.normalized-events/v1"`),
  `src/ccswitch/worktrees.py` (`ccswitch/<branch>-<short>` child branches and the
  `<repo_root>/.ccswitch/worktrees/…` layout), `server/sessions/orchestrator.py` (the
  `".ccswitch" not in cwd_path.parts` delete guard), `server/aio.py` and
  `server/ipc/drain_on_signal.py` (literal `ccswitch-daemon.service`), the 18 unit files under
  `deploy/`, `web/src/pipeline/notifyPref.ts` (the existing migrate-on-read helper Phase 2b must
  reuse), `web/src/lib/repoFromCwd.ts`, `.claude/agents/ccswitch-{implementer,reviewer}.md` and
  `.agents/roles/ccswitch-{implementer,reviewer}`.
