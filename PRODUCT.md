# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One primary user: **the single operator who built and runs ViberPit** on their own tailnet
(confirmed 2026-09-29). Design for an expert who knows the system, not for a newcomer.

The same person wears several hats, and the navigation is organised around the question each
hat is asking rather than around accounts or permission levels
(`docs/product/features/web-dashboard-mobile.md` § Modes):

- **Developer / vibe coder** — "What needs me right now? What are my sessions and PRs doing?"
- **Product manager** — "What are we building, how does it fit together, in what order?"
- **Project manager** — "What is in flight, blocked, or waiting on a human? Is delivery healthy?"
- **Operator / admin** — "Accounts, hosts, deploy, config."

Situations: at the desk on a wide screen; on a phone or tablet anywhere on the Tailscale network,
often one-handed; and a wall-mounted **kiosk** display that nobody touches (`?kiosk=1`, read-only,
ten-foot type scale). Mobile is still the web app — there is no native app and none is planned.

**Routine jobs** (confirmed): (1) *answer what needs me* — permission cards, decisions, holds,
hand-backs — so agents keep moving; (2) *start and steer sessions* — write prompts, dispatch work,
read transcripts, redirect an agent. Watching delivery health (PR lanes, CI, automations) and
planning the roadmap are real surfaces but not the routine reason the app gets opened.

Other audiences are **not** design inputs: no multi-user or multi-tenant UI, no public-internet
exposure (`web-dashboard-mobile.md` non-goals). Tailnet peers on the allowlist may drive sessions,
but they are trusted extensions of the operator, not a second persona.

## Product Purpose

ViberPit runs and steers live AI coding-agent sessions across several accounts, engines and hosts,
with usage-aware account rotation, isolated per-session workspaces, and governed delivery from
backlog to merged PR (`docs/product/README.md`). It exists because one Claude Max account hits
session and weekly limits quickly, and running many live agent sessions from terminals ties the
operator to one machine (`features/multi-account-management.md`, `features/web-dashboard-mobile.md`).

Success, per the founding web spec (`specs/001-*/spec.md`): the operator **never experiences
a hard service interruption**, and manages everything **from a single screen reachable from their
phone**.

## Positioning

**The whole loop in one place** (confirmed 2026-09-29): *Plan · Build · Test · Ship · Repeat* —
roadmap, sessions, pipeline and delivery live in one shell, not four tools. A neighbouring product
(an engine's own agents view, a kanban-for-agents, a terminal multiplexer) covers one stage; ViberPit
owns the hand-offs between them.

Supporting mechanisms, in the order they matter to the operator: governed autonomous delivery
(backlog runner → sessions → PRs → auto-merge under SDLC governance, hold ladders and evals);
usage-aware multi-account rotation; steer-from-anywhere over the tailnet.

## Operating Context

- **Access:** loopback-bound server behind `tailscale serve` (or nginx with a `tailscale cert`);
  every route and WebSocket needs a verified tailnet identity on the allowlist. HTTPS is required for
  browser features such as the microphone. Never on the public internet.
- **Shape of the day:** many sessions at once, most of them dispatched by automations rather than
  typed by hand. The operator is interrupt-driven: notifications (web push) and "needs you" surfaces
  pull them in; they clear the queue and leave. Long desk sessions happen, but glance-and-act from
  the phone is the common case.
- **Engines and accounts:** Claude (each account an isolated `CLAUDE_CONFIG_DIR`), OpenAI Codex,
  GitHub Copilot (experimental). An account is one engine-scoped identity; rotation is by headroom.
- **Hosts:** an edge host plus worker hosts running the same session daemon (multi-host fleet); a
  deploy controller ships the app itself. Restarts are never "a brief blip" — they touch live sessions.
- **Systems of record:** GitHub (issues, PRs, checks, projects) is where work is tracked; ViberPit
  reads and drives it. Sessions, transcripts and usage are ViberPit's own records.
- **Sections the operator sees** (`web/src/shell/navDefs.ts`): **Sessions** (live + archived,
  composer, permission cards, background scope), **Pipeline** (PR health per repo, lane by lane; the
  only section allowed on the kiosk), **Roadmap** (products → capabilities → initiatives → issues,
  plus the Inbox of decisions waiting on the operator), **Delivery** (automations and CI health),
  **Settings** (accounts, rotation, hosts, preferences — 20 sections in 5 groups).
- **Layout modes:** phone, tablet, desktop, kiosk — one `AppShell` and one route table drive all
  four; there is no mobile/desktop fork.

## Capabilities and Constraints

- Live session control: start, prompt, read transcript, reply, interrupt, permission answers, model
  switching (upgrades park for human approval), archive/restore/delete; background sessions; child
  sessions the operator steers independently.
- Automations: backlog runner, PR doctor, CI repair, deploy controller, triage inbox; each can hold,
  hand back or propose a decision. Sessions signal completion with `mark_complete` / `mark_no_pr` /
  `hand_back`.
- Governance is a product feature, not plumbing: constitution principles, managed deny rules, hooks,
  review policy and an eval corpus gate agent behaviour; the UI must make holds and gates legible.
- Constitution constraints that shape UI (`.specify/memory/constitution.md`): never fork or patch a
  native agent binary (I); one engine-scoped identity per account, strict isolation (II); verify
  against the real binary/API (III); security-first, exposure-conditional auth (IV); single source of
  truth, no dead code (VI).
- **Terminology that is binding:** *session* (one live agent run), *account* (one engine-scoped
  identity), *engine* (Claude / Codex / Copilot), *automation*, *hold*, *hand-back*, *needs you*.
  A session's **turn** being over is "done"; the **objective** being met is "complete" — status
  surfaces never say "Done" for a session. Sessions belong to a **repo** and run in isolated
  **worktrees**.
- Performance and accessibility are contracts with budgets (`docs/PERFORMANCE.md`,
  `docs/ACCESSIBILITY.md`); the frontend has a bundle budget check.
- **Explicitly undecided:** whether and when the product opens up beyond the operator. The
  approved brand copy says "open source cockpit for builders", but the repository is private with
  no licence today; until that changes, newcomer onboarding and public-facing surfaces are out of
  scope and must not be inferred from the tagline.

## Brand Commitments

Owner-approved, from `docs/product/guides/viberpit-rebrand.md` § Visual identity and
`src/viberpit/brand.py` (the one place the name is spelled):

- **Name:** ViberPit (renamed from its original slug in September 2026; the old spelling survives
  only as compat aliases and a ratcheted inventory — `docs/product/guides/viberpit-rebrand.md`).
- **Tagline:** *Code it. Vibe it. Ship it.* **Positioning line:** *The open source cockpit for
  builders who'd rather create than click around.* **Loop:** *Plan · Build · Test · Ship · Repeat.*
  **Alternates:** *Ideas to impact. On autopilot.* / *Your product lifecycle, less work.* / *Build
  more. Manage less.* / *From concept to commit.* / *Serious ideas. Less serious attitude.* / *Less
  grind. More build.*
- **Voice:** serious about the work, not about itself ("Serious ideas. Less serious attitude.").
- **Assets** (`web/public/brand/`): `viberpit-mascot.png` (viper in sunglasses at a laptop — source
  of every icon), `viberpit-logo-horizontal.png` (README), `viberpit-logo-stacked.png` (docs,
  social); derived app icons under `web/public/icons/` and `favicon.png` / `apple-touch-icon.png`
  via `web/scripts/gen-icons.sh`. The in-app mark is `web/src/shell/BrandMark.tsx`; the wordmark is
  rendered as text from `BRAND_NAME_PARTS` in `web/src/brand.ts`, never as an image.
- **Binding decisions already made:** brand colours (Viper Green, Vibe Purple) are logotype-only
  and are *not* UI tokens — they fail AA for text; the interactive accent and status tones are
  separate. Fonts stay system (`system-ui`) — the brand sheet's Poppins / Inter / Caveat live only in
  the logo rasters.

## Evidence on Hand

- Real, current feature documentation with linked PRs: 39 files under `docs/product/features/`,
  hierarchy in `docs/product/README.md`; founding web spec `specs/001-*/`; design records
  under `docs/superpowers/specs/`; audits under `docs/audits/`.
- Real data in the running app: live sessions, transcripts, usage and reset times, PR lanes, CI
  health, automations. Screenshots for design work come from the deployed loopback instance.
- **Absent — do not fabricate:** customers, testimonials, case studies, press, benchmarks, pricing,
  licensing terms, public repository stats (the repository is private with 0 stars/forks), download
  counts, or any "trusted by" claim.

## Product Principles

1. **The queue is the product.** What needs the operator — a card, a hold, a hand-back, a decision —
   is found first on every surface and every device; everything else is context for acting on it.
2. **One shell, every hat, every screen.** Phone, tablet, desktop and kiosk share one shell and one
   route table; a hat is a question the nav answers, never a separate app or a permission level.
3. **Never a hard interruption.** Rotation, continuity and recovery are invisible until they matter,
   then they are explicit and honest — a session's true state outranks a reassuring label.
4. **Trust through gates, not vigilance.** Agents act inside governance; the operator decides at the
   gates. The UI makes holds, approvals and their reasons legible instead of asking the human to watch.
5. **Built by and for the one operator.** Density, speed and glanceability beat explanation; expert
   defaults over onboarding; docs carry the teaching so the chrome doesn't have to.

## Accessibility & Inclusion

Every page and shared component targets **WCAG 2.1 AA in full, no exceptions**
(`docs/ACCESSIBILITY.md`): landmarks and live regions per shell region, `jest-axe` in the unit
tests, keyboard-reachable everything. Touch targets meet **WCAG 2.2 AA 2.5.8** (44 px) on coarse
pointers (`web/e2e/touch-targets.spec.ts`). `prefers-reduced-motion` is honoured across the shell
and motion helpers. The kiosk mode enlarges the whole type scale for ten-foot readability. Brand
colours are excluded from text and interactive use because they fail contrast (see Brand
Commitments).
