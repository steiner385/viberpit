# Hold cleanup — releasing the AI-written needs_human holds (2026-09-18)

Decision 1 of `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` (§10). On 2026-09-12 a Codex
session's sub-agent set 37 ViberPit issues to `triage_state = needs_human` through the overlay API as a
side effect of a RICE scoring pass (`2026-09-12-viberpit-backlog-priorities.md`). The API recorded them as
human decisions and nothing expired them. This cleanup releases the evidence-class rows so the Backlog
Runner's grouping can batch them; the decision-class and owner-only rows stay held until the ladder lands.

Applied 2026-09-18 01:20 UTC on dobby via `PATCH /api/pm/work-items/{id}/overlay` (`lan@local`), one
read-back before and after each write. RICE inputs were unchanged on every row (asserted). Journal:
`/tmp/hold-cleanup-journal.json` on dobby; reversible by writing `triageState = needs_human` back to the
released ids.

| Class | Count | Rule |
|---|---:|---|
| released → `classified` | 28 | `auto-error` label, or the audit's "verify" holds (#1477, #1495), or the epic #997 |
| kept `needs_human` | 8 | decision-class (#1502, #1517, #1524, #1540, #1545) and owner-only (#1579, #1592, #1594) |

## Released

| Issue | Title | classified_by before → after |
|---|---|---|
| #997 | Epic: 003 GitHub-native planning — Phase B rollout, Phase C consumers, | human → human |
| #1477 | Feature.pr_number is never written, so the roadmap's 'proposed in PR # | human → human |
| #1483 | [auto-error] WARNING server.auth.identity: ignoring tailscale identity | human → human |
| #1484 | [auto-error] WARNING ccswitch.accounts: usage_for(tony_cairnea.com): t | human → human |
| #1487 | [auto-error] WARNING client: ws gap detected — healing transcript via  | human → human |
| #1488 | [auto-error] WARNING client: transcript.liveness.stale — rev advanced  | human → human |
| #1489 | [auto-error] WARNING client: ws closed code=<n> | human → human |
| #1490 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1491 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1492 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1495 | accept_bulk re-proposes siblings outside its begin_nested savepoint | human → human |
| #1506 | [auto-error] WARNING server.ipc.client: ipc: events ws error (timed ou | human → human |
| #1507 | [auto-error] WARNING server.sessions.host_probe: fleet.probe_failed ho | human → human |
| #1509 | [auto-error] WARNING ccswitch.accounts: usage_for(tony_cairnea.com, al | human → human |
| #1510 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1511 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1512 | [auto-error] WARNING ccswitch.accounts: usage_for(tony.claude_steinonl | human → human |
| #1513 | [auto-error] WARNING server.automations.cicd_doctor: cicd rollup behin | human → human |
| #1514 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1515 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1538 | [auto-error] WARNING ccswitch.accounts: usage_for(tony.claude_steinonl | human → human |
| #1567 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1570 | [auto-error] WARNING client: ws error | human → human |
| #1571 | [auto-error] WARNING server.obs.loop_lag: loop_lag: event loop blocked | human → human |
| #1573 | [auto-error] WARNING server.sessions.orchestrator: session.permanent_d | human → human |
| #1574 | [auto-error] WARNING server.sessions.orchestrator: chain_ingest: sessi | human → human |
| #1575 | [auto-error] WARNING ccswitch.accounts: usage_for(tony_cairnea.com): s | human → human |
| #1576 | [auto-error] WARNING server.sessions.remote: usage rpc for tony_cairne | human → human |

## Kept

| Issue | Title |
|---|---|
| #1502 | audit: in-memory state delivered only on a WS attach can be stranded s |
| #1517 | fleet capacity: alarm on a host at cap, drain completed records, and m |
| #1524 | Backlog Runner: derive the effective repo list once so empty enabled_r |
| #1540 | Backlog Runner: human hand-back after Phase D — un-park a gave_up row  |
| #1545 | sdlc: materialization follow-ups — retry after the attempt cap, epic a |
| #1579 | docs(sdlc): spec for UI E2 — accessible and consistent interaction |
| #1592 | ViberPit Phase 7 (hosts): remove ccswitch-* unit aliases, path symlink |
| #1594 | ViberPit rebrand: legacy spelling in governance-enforcement files need |

Note: `classified_by` still reads `human` after the release because the overlay API stamps every
caller as human. Spec §8 fixes that; this cleanup does not pretend otherwise.
