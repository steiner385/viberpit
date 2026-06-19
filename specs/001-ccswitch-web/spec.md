# Feature Specification: ccswitch — Multi-Account Claude Session Manager (Web)

**Feature short name:** ccswitch-web
**Created:** 2026-06-19
**Status:** Draft
**Constitution:** Governed by `.specify/memory/constitution.md` v1.0.0 (Principles I–VII)

## Summary

A web-hosted, mobile-accessible interface for running and managing multiple live Claude Code
sessions spread across several Claude Max accounts. The platform tracks each account's exact
remaining usage (rolling-session and weekly windows), routes new sessions to the account with the
most headroom, and automatically moves a running session to another account the moment its current
account hits a usage limit — so the user never experiences a hard service interruption. The user
manages everything (list, open, rename, close, create sessions; see usage; switch accounts) from a
single screen reachable from their phone.

A command-line foundation already exists (account capture, usage tracking, account selection,
single-session auto-rotation); this specification covers the complete product, with the web
interface as the primary surface.

## User Scenarios & Testing

### Primary user story

As someone who holds multiple Claude Max subscriptions, I want a single place — usable from my
phone — where I can see all my running Claude sessions across all my accounts, jump between them,
start new ones, and keep working without interruption, because the system silently shifts me to an
account that still has capacity whenever one runs out. I should never have to manually figure out
which account has quota left or copy credentials around.

### Acceptance scenarios

1. **Given** I have three accounts registered, **when** I open the web interface on my phone,
   **then** I see each account's current session-window and weekly usage and a list of all my
   sessions, each labeled with the account it is running on.
2. **Given** I am viewing the session list, **when** I create a new session, **then** the system
   starts it on the account with the most remaining headroom and opens it for interaction.
3. **Given** I am interacting with a session whose account reaches its usage limit, **when** the
   limit is hit, **then** the system moves that session to another account with headroom and the
   session continues, with the displayed account label updating to reflect the change — without me
   taking any action.
4. **Given** I have several sessions, **when** I select a different session from the list, **then**
   I am switched into that session and can interact with it.
5. **Given** a session I no longer need, **when** I close it, **then** it stops and is removed from
   the list.
6. **Given** a session with an unhelpful name, **when** I rename it, **then** the new name is shown
   everywhere that session appears.
7. **Given** I am not authenticated, **when** I attempt to reach any part of the interface or its
   data, **then** I am denied access until I log in.
8. **Given** I register a new account that signs in through a third-party identity provider (e.g.
   Google), **when** I complete that provider's login in the browser, **then** the account is
   captured and becomes usable for sessions with no further credential handling on my part.
9. **Given** all of my accounts have reached their limits, **when** I try to start or continue a
   session, **then** I am clearly told the earliest time capacity will return (in my local time),
   rather than failing silently.

### Edge cases

- An account's stored authorization expires or is revoked → it is marked as needing re-login,
  excluded from selection, and surfaced to the user; other accounts continue working.
- A brand-new account with zero recent usage (no active usage window) → displayed correctly as
  full headroom, not as an error.
- The connection between the phone and the interface drops mid-session → on reconnect, the user
  returns to the same sessions with recent context intact; no session is lost.
- Two new sessions are requested at nearly the same time → they are placed on different accounts
  when headroom allows, not stacked onto the same one.
- A session ends on its own (the underlying process exits) → it is shown as ended and remains
  visible until dismissed, so the user can see it finished rather than having it vanish.
- The interface is opened on a small mobile screen → all management actions remain reachable and
  legible.

## Requirements

### Functional Requirements

**Account management**

- **FR-001**: The system MUST let a user register multiple Claude Max accounts, each with a
  user-chosen label.
- **FR-002**: The system MUST capture an account's credentials by guiding the user through that
  account's normal browser login, including logins federated through a third-party identity
  provider, without the user manually handling credential material.
- **FR-003**: The system MUST keep each account's identity and credentials isolated from every
  other account, such that activity on one account never consumes or affects another.
- **FR-004**: The system MUST let a user view all registered accounts with their current status and
  remove an account.
- **FR-005**: The system MUST warn when a registered account is not a Max subscription.

**Usage tracking & account selection**

- **FR-006**: The system MUST display each account's exact current usage for both the
  rolling-session window and the weekly window, with the time each window resets shown in the
  user's local time zone.
- **FR-007**: The system MUST refresh usage on demand and keep displayed usage reasonably current
  without the user manually triggering it.
- **FR-008**: When starting a session without a specified account, the system MUST select the
  account with the most remaining headroom and MUST exclude any account that has reached its limit
  or needs re-login.
- **FR-009**: The system MUST prevent two concurrently-started sessions from being assigned to the
  same account when another account with headroom is available.
- **FR-010**: When all accounts have reached their limits, the system MUST report the earliest time
  capacity returns (in local time) instead of failing silently.

**Session lifecycle & rotation**

- **FR-011**: The system MUST allow multiple live Claude sessions to run simultaneously, each
  associated with a specific account.
- **FR-012**: The system MUST let the user create, open, rename, and close sessions.
- **FR-013**: The system MUST automatically move a running session to another account with headroom
  when its current account reaches a usage limit, preserving the session's ongoing work, and MUST
  reflect the account change in the session's displayed label.
- **FR-014**: The system MUST keep a session that is not currently in focus running in the
  background (its work continues; it is not suspended).
- **FR-015**: The system MUST persist the set of sessions so that reopening the interface shows the
  still-running sessions and re-associates with them.

**Web interface**

- **FR-016**: The system MUST present a single screen listing all sessions across all accounts,
  each showing its name, the account it is on, its status, and that account's usage.
- **FR-017**: The system MUST let the user navigate the session list and switch into any session to
  interact with it.
- **FR-018**: The system MUST let the user interact with a session live from the browser, including
  on a mobile device.
- **FR-019**: The system MUST reflect session state changes (status, usage, account rotation) in
  near-real-time without a manual page refresh.
- **FR-020**: The interface MUST be operable on a mobile-phone screen, with all management actions
  reachable.

**Access & security**

- **FR-021**: The system MUST require authentication for every interface route and every data or
  session channel; there MUST be no anonymous access.
- **FR-022**: The system MUST NOT be reachable on a public network interface by default; the
  default reachable surface is a private network path.
- **FR-023**: The system MUST never expose credential or token material in its interface, logs, or
  error messages.
- **FR-024**: The system MUST be deployable to a remote host by configuration alone (no code
  changes), retaining the same authentication guarantees.

### Key Entities

- **Account**: a registered Claude Max subscription — label, status (ok / limited / needs-relogin /
  not-max), and current usage snapshot (session %, weekly %, reset times). Holds isolated
  credentials (not exposed in the interface).
- **Usage Window**: a measured usage figure for an account — kind (rolling-session or weekly),
  utilization percentage, and reset time.
- **Session**: a live Claude conversation — id, user-chosen name, the account it currently runs on,
  status (running / working / idle / limited / ended), and recent context for re-entry.
- **User**: the authenticated operator of the interface — credential for login; the sole party
  permitted to manage accounts and sessions.

## Non-Functional Requirements

- **NFR-001 (Continuity)**: Account rotation MUST NOT lose a session's accumulated work; the user
  experiences at most a brief, recoverable interruption, not a lost session.
- **NFR-002 (Security posture)**: The system MUST be safe to eventually host on a public platform —
  strong authentication, transport encryption at the platform edge, and secrets supplied by the
  environment rather than committed — without a security rewrite.
- **NFR-003 (Credentials at rest)**: Stored credential material MUST be protected at rest (private,
  least-exposure storage) and isolated per account.
- **NFR-004 (Accessibility)**: User-facing surfaces MUST meet WCAG 2.1 AA — keyboard operability,
  sufficient contrast, semantic structure, visible focus, and reduced-motion support.
- **NFR-005 (Responsiveness)**: Interactive latency MUST feel immediate on a typical mobile
  connection; live session output streams without perceptible lag under normal conditions.
- **NFR-006 (Reliability of rotation)**: Limit detection and rotation MUST act on authoritative
  signals so the user is not stranded on an exhausted account when another has capacity.
- **NFR-007 (Quality gates)**: The product MUST meet the constitution's testing gates — TDD,
  full unit-test pass, and ≥85% coverage with negative cases on account-selection, credential, and
  authentication logic.
- **NFR-008 (Integration boundary)**: The product MUST integrate with Claude only through supported
  surfaces (no forking or patching of the Claude binary), per constitution Principle I.

## Success Criteria

- **SC-001**: A user can go from opening the interface to interacting with a session on the
  highest-headroom account in under 15 seconds.
- **SC-002**: When an account hits its limit during active use, the session continues on another
  account within 30 seconds and with no loss of conversation history, in at least 95% of rotations.
- **SC-003**: The displayed per-account usage matches the provider's actual usage (no estimation
  error), because it is sourced from authoritative usage data.
- **SC-004**: A user can register a new account end-to-end (including third-party-federated login)
  in under 2 minutes, after which it is immediately usable for sessions.
- **SC-005**: All core management actions (open, switch, rename, close, create) are completable on a
  mobile phone screen without horizontal scrolling or obscured controls.
- **SC-006**: 100% of interface routes and data/session channels reject unauthenticated access.
- **SC-007**: With three accounts registered, a user can sustain continuous work that exceeds any
  single account's session-window limit, with no manual account switching, across at least one full
  rotation cycle.
- **SC-008**: After a dropped connection, the user is reconnected to their existing sessions with
  recent context within 10 seconds of network recovery.

## Assumptions

- **Single operator**: the interface serves one authenticated user (the account owner); multi-user
  roles are out of scope.
- **Account count**: designed for a small number of accounts (e.g. 3) but not hard-limited to three.
- **Initial access path**: reached over a private network (e.g. a personal VPN/tailnet) from the
  user's phone; public hosting is a later, explicitly-hardened step.
- **Initial in-session experience**: the first release renders a session as a faithful live
  terminal view; a richer structured (message/tool-card) view is a later enhancement, not part of
  the first release.
- **Provider usage data**: the provider exposes authoritative per-account usage for the session and
  weekly windows; the system relies on this rather than estimating.
- **Decided technology** (recorded for context; not part of the requirements): a Python async web
  backend, an SQL datastore, and a modern web frontend with an in-browser terminal, per the
  constitution's Technology & Architecture Constraints. The CLI foundation already exists in the
  repository.

## Dependencies

- The Claude Code command-line application must be installed on the host that runs sessions, and
  must support per-process configuration isolation, programmatic usage retrieval, and session
  resume (these have been verified against the current version per constitution Principle III).
- A private network path between the user's device and the host (initial deployment).
- Valid Claude Max subscriptions for each account the user registers.

## Out of Scope

- Forking, patching, or otherwise modifying the Claude Code binary (prohibited by Principle I).
- A structured, claude.ai/code-style conversation view (messages, tool cards, diffs) — deferred to
  a later phase; the first release uses a terminal view.
- Public-internet hosting in the first release (the architecture is kept portable for it, but
  hardened public exposure is a later step).
- Multi-user accounts, roles, or sharing.
- Splitting a single session's sub-agents across different accounts (a session and its sub-work
  belong to one account by design).
