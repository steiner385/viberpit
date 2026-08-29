# Multi-host runner dispatch + runner onboarding — Design (sub-project 4)

**Date:** 2026-08-29
**Status:** Draft (brainstorm) → awaiting user review before writing-plans
**Parent:** `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` — this is the
detailed design for that umbrella's **sub-project 4, "Host registry + dispatch policy"** ("the
genuinely new capability"). Sub-projects 1a (CI retention), 1b (Postgres), 2 (rocky provisioned),
and 3 (shared credential mount) have shipped and are live in production.

## Problem

`dobby` "still isn't showing up as one of the available runners." Investigation on the live systems
(2026-08-29) found the reason is deeper than a missing config row:

- The **host registry** (`Host` model, `server/db/models.py:829-861`), the **health-probe loop**
  (`server/sessions/host_probe.py`), the **ranking** (`server/sessions/fleet.py`,
  `rank_hosts`/`score_host`), the **`FleetRouter`** (`server/sessions/fleet_router.py`), and the
  **Fleet UI/API** (`server/api/fleet.py`, `web/src/components/HostBadge.tsx`) all exist.
- But **dispatch is not wired to any of it.** `FleetRouter` is never instantiated anywhere in
  `server/` (a repo-wide search for `FleetRouter(` outside tests returns nothing); `rank_hosts()`
  and `.pick()` are only referenced inside `fleet_router.py` itself. Session creation
  (`server/api/sessions.py:584`) spawns via a **single** `app.state.sessions =
  RemoteOrchestrator(DaemonClient(primary_endpoint))` (rocky) and merely **stamps**
  `record.host = app.state.host_name` ("rocky") as a label — there is no host *selection*.

So today **every session goes to rocky**, and the config comment's "it's a config flip, not a code
change" (`server/config.py:120-122`) is half-true: the ranking code is N-host, but it is dead and
unconnected. Adding a registry row for dobby would make it appear in the Fleet UI and get
health-probed, and **zero sessions would ever route to it.**

## Verified topology (2026-08-29)

| Fact | State |
|---|---|
| Edge (`ccswitch.service`, :8787) | on **dobby** |
| Session daemon (`ccswitch-daemon.service`, :9443) | on **rocky**, active + enabled |
| Fleet registry | contains **only `rocky`** (auto-seeded from `CCSWITCH_SESSIOND_ENDPOINT`) |
| dobby daemon | unit installed, **inactive + disabled** (deliberately, post-cutover) |
| dobby TLS certs | `ca.pem` + `client.*` only — **no server cert** |
| Shared DB | Postgres on dobby (`100.102.11.121:5432`); rocky's daemon connects over the tailnet |
| Shared secret | `CCSWITCH_MASTER_KEY` (decrypts account tokens) — must match across hosts |
| Account store | canonical on dobby; rocky mounts it via `ccswitch-mounts.service` |
| Resources | dobby 16 cores / 19 GB · rocky 8 cores / 31 GB (load ~10) |
| Cert tool | `scripts/ccswitch-mint-mtls.sh` |

**Consequence of the shared DB:** all DB-backed reads (session list, dashboard, usage, account
rotation state) are **already fleet-wide**. No fan-out/merge is needed for them. Only **live,
in-daemon-memory** operations must be routed to the owning host.

## Decisions (this sub-project)

1. **Dispatch = auto load-balance + manual override.** New sessions are dispatched by
   `FleetRouter.pick()` (ranked by the shipped headroom 0.5 / mem 0.3 / load 0.2 × weight score),
   with an optional per-session host preference that moves a chosen host to the front but still
   fails over. Confirmed with user 2026-08-29.
2. **dobby is enabled as a runner with a session cap.** `max_sessions` protects the edge (which also
   serves UI/config/reauth/deploy) from session starvation. Cap value chosen at onboarding time.
3. **dobby is reached over https/mTLS, uniform with rocky** — endpoint
   `https://dobby.tail0dd570.ts.net:9443`, its own minted server cert (SAN = the tailnet identity),
   a dedicated `EnvironmentFile` for the daemon. Chosen over a local unix socket for routing/probe
   uniformity (no unix special-casing) and future flexibility. Confirmed with user 2026-08-29.
4. **Repo-locality-aware ranking is deferred.** The umbrella's own analysis (open question 1) shows a
   hard locality filter is self-defeating (`ensure_clone` is lazy + `--reference-if-able`). This
   sub-project keeps the shipped headroom/mem/load score; soft locality preference is a documented
   future enhancement. Confirmed with user 2026-08-29.
5. **Global/cross-cutting ops stay pinned to the primary host** (automations, backlog runner, reauth,
   git-credential alerts, pipeline history). "Primary host" = the host resolved from
   `CCSWITCH_SESSIOND_ENDPOINT` at startup (today rocky). Balancing those across hosts is out of
   scope for adding a second runner (YAGNI).

## Architecture

### The gap to close

The edge talks to sessions through **one** object: `app.state.sessions` (a `RemoteOrchestrator` in
remote mode, or an in-process adapter otherwise). Every API route reaches it
(`request.app.state.sessions`). To run sessions on more than one host, session-scoped calls must go
to the **owning** daemon while everything else keeps working unchanged.

### `FleetOrchestrator` (new)

Introduce a `FleetOrchestrator` that implements the **same interface** as `app.state.sessions` and
wraps a `FleetRouter`. It becomes `app.state.sessions` whenever the registry holds more than one
reachable host; a single-host deployment keeps today's direct `RemoteOrchestrator` (rollback-safe,
zero behavior change — mirrors umbrella cross-cutting "Rollback").

Method routing, by call shape:

- **`create_session` / spawn** → `FleetRouter.pick(prefer=<optional host>)` → chosen host's
  orchestrator → spawn → stamp the **chosen** host onto the session row (replacing the current
  static `app.state.host_name` stamp at `server/api/sessions.py:580-595`). Cascades to the next
  ranked host on spawn failure; raises `NoHostAvailable` rather than falling back in-process (the
  split-brain guard the umbrella cross-cutting section and `deploy/ccswitch.service.d/remote.conf`
  require).
- **Per-session live ops** (open/attach stream, send input, resize, stop/kill, live status &
  current buffer, X11/VNC/browser-open) → resolve the owning host from `session.host` (shared DB) →
  route to that host's orchestrator via `FleetRouter.get(name)`. A cache keyed by `session_id →
  host` avoids a DB read per keystroke; it is authoritative from the DB on miss.
- **DB-backed reads** (session list, dashboard state, usage, account state) → **unchanged**; they
  read the shared DB directly and are already fleet-wide.
- **Global/pinned ops** (decision 5) → routed to the primary host's orchestrator.

`FleetRouter` today (`server/sessions/fleet_router.py`) already owns `{name: orch}`, builds
`candidates()` from the registry (filtered to hosts it holds a client for), and `pick()`s a ranked,
prefer-aware order. This design **wires it in** and **populates it** — making currently-dead code
live (satisfies the "no half-dead code" working norm; the old single-orchestrator create path is
deleted in the same PR, not left beside the new one — Principle VI).

### Router population & registry sync

At startup, `FleetOrchestrator` builds one `RemoteOrchestrator(DaemonClient(endpoint, edge-client-
TLS))` per **enabled** `Host` row and registers it via `FleetRouter.add(name, orch)`. The edge's
client TLS env (`CCSWITCH_SESSIOND_CLIENT_CERT/KEY/CA`) is reused for every https host — one CA
signs all daemon server certs, so no per-host client config is needed.

To honor "operator Fleet-UI changes survive without an edge restart" (the registry-seeding
contract), the router **reconciles lazily**: before each `pick()` / per-session route it ensures a
client exists for every currently-enabled host and drops clients for removed/disabled hosts.
`DaemonClient` is a lazy async HTTP client, so building one is cheap and holds no socket until first
use.

### Protocol / health gating (no whole-edge hard-fail)

Today the edge **hard-fails at startup** if the primary daemon's protocol is incompatible
(`server/app.py` split-brain guard). Under N hosts that must become **per-host**: a host whose probe
records an incompatible `protocol`, or `healthy != True`, or `active_sessions >= max_sessions`, is
simply **not dispatchable** (`_dispatchable()` in `server/sessions/fleet.py:45` already excludes
`healthy is None`/`False` and at-cap). One bad host must never take down the edge or hide the others
(`host_probe.py` already probes hosts independently). The primary host retains a startup
compatibility check so a fully-incompatible fleet still fails loud.

### Manual override (UI + API)

- **API:** `POST /api/sessions` (and automation/create paths that mint sessions) accept an optional
  `host` field, forwarded as `prefer` to `pick()`. Unknown host → `UnknownHost` surfaced as 400.
- **UI:** a "Runner: Auto ▾" dropdown on the create-session dialog, defaulting to **Auto**,
  populated from `GET /api/fleet/hosts` (enabled hosts). A standard control — no new design token.

### Session cap

dobby is registered with `max_sessions = <cap>` (via Fleet UI `PUT /hosts/{name}` or
`CCSWITCH_FLEET_HOSTS` seed + operator edit). `_dispatchable()` already excludes an at-cap host, so
the cap is enforced by ranking for free once dispatch is wired. The probe writes `active_sessions`,
so the cap reflects live occupancy.

## Runner onboarding — the "standard work"

A `docs/` runbook + a copy-paste checklist capturing the concrete prerequisites a runner needs
(verified against rocky, the working reference runner). Ordered so each step is verifiable before the
next:

1. **Network:** host on the tailnet with `tailscaled` running; `https://<host>:9443` reachable from
   the edge. Postgres on dobby must permit the host: bind the tailnet address and add a `pg_hba`
   `/32` rule for the host's tailnet IP + the `ccswitch` role/db (umbrella decision 7 — the PG wire
   is cleartext, so the tailnet is the encryption layer; no LAN listener).
2. **Runtime:** repo checkout + built venv at a known path; `claude`, `node`, `git`, `python3`,
   Xvfb present; `ccswitch-daemon.service` (remote variant, `deploy/ccswitch-daemon-remote.service`)
   installed.
3. **mTLS:** mint a **server** cert for the host with `scripts/ccswitch-mint-mtls.sh`, SAN = the
   tailnet identity (`DNS:<host>.tailnet, IP:<tailnet-ip>` — **not** `<host>.lan`; hostname mismatch
   is the top documented footgun). Install cert/key + the shared CA. The edge already trusts the CA,
   so no edge change beyond registering the host.
4. **Daemon env** (`/etc/ccswitch/daemon.env`, a **dedicated** file — see the dobby collision note):
   `CCSWITCH_DB_URL` → the shared Postgres (dobby tailnet IP:5432); **matching**
   `CCSWITCH_MASTER_KEY`; `CCSWITCH_SESSIOND_ENDPOINT=https://<host>.tailnet:9443`;
   `CCSWITCH_SESSIOND_TLS_CERT/KEY/CA`; `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge`;
   `CCSWITCH_ENABLE_XVFB=true`.
5. **Account store:** mount each account's `cfg/<account>/` (and shared `.claude`) from dobby, as
   rocky's `ccswitch-mounts.service` does — canonical creds shared, never copied (umbrella decisions
   4-5). A host that is *itself* dobby needs no mount (it is the source).
6. **Start:** `systemctl enable --now ccswitch-daemon.service`.
7. **Register:** add the host to the fleet (Fleet UI `POST /hosts` or the `CCSWITCH_FLEET_HOSTS`
   seed) with its endpoint, `enabled=true`, and a `max_sessions` cap.
8. **Verify:** probe shows `healthy=true`, `sha` matches the edge, `protocol` matches; then a
   smoke-test session created with `host=<new>` (manual override) actually lands there
   (`HostBadge` shows the host; the daemon shows the PTY).

### dobby-specific application (the immediate goal)

dobby is the edge host, so most prerequisites are already satisfied — with two exceptions and one
gotcha:

- **Missing:** a **server** cert (dobby holds only a client cert today). Mint one, SAN
  `DNS:dobby.tail0dd570.ts.net, IP:100.102.11.121`.
- **Gotcha — env collision:** dobby's `ccswitch-daemon.service` currently shares the **edge's**
  `/etc/ccswitch/ccswitch.env`, where `CCSWITCH_SESSIOND_ENDPOINT=https://rocky:9443` is the edge's
  *target*. If the daemon reads that as its *bind*, it collides. **Resolution (decision 3):** point
  dobby's daemon unit at a dedicated `/etc/ccswitch/daemon.env` with
  `CCSWITCH_SESSIOND_ENDPOINT=https://dobby.tail0dd570.ts.net:9443` + its server certs, so
  edge-target and daemon-bind never collide.
- **Already satisfied:** shared DB (local Postgres), `CCSWITCH_MASTER_KEY`, account store (dobby is
  the source — no mount), Xvfb availability (verify `CCSWITCH_ENABLE_XVFB`).

Then register `dobby=https://dobby.tail0dd570.ts.net:9443` with a `max_sessions` cap and verify.

## Testing (Principle V, TDD)

Unit-testable without two live hosts (fake registry rows + fake per-host orchestrators):

- `FleetOrchestrator.create` picks the top-ranked host; cascades to the next on spawn failure;
  raises `NoHostAvailable` (never in-process) when none are dispatchable.
- Per-session op routes to the host recorded in `session.host`; cache miss falls back to the DB.
- `prefer=<host>` moves it to the front but still fails over; unknown `prefer` → error.
- An at-cap / unhealthy / protocol-mismatched host is excluded from `pick()` but does not break the
  edge (reuse `fleet.py`/`fleet_router` tests + `host_probe` independence).
- Single-host registry → `FleetOrchestrator` degrades to the direct path (no behavior change).
- Web: `vitest` for the runner dropdown; `tsc --noEmit`; `eslint`.

Full gate before push (per CLAUDE.md): `ruff check src/ server/`, `mypy server/`, `pytest tests/`,
`pytest server/tests/`, plus the web checks. Adding **no new model/column** (the `Host` model and
`session.host` already exist), so the Postgres-suite / reconcile-list obligations do not apply — to
be re-confirmed during implementation.

## Rollout (spec-first → approval → code PR → ops)

1. Land the dispatch wiring as **one PR** (FleetOrchestrator + routing + manual override + tests).
   While the registry still holds only rocky, `FleetOrchestrator` degrades to the current path —
   **no production behavior change on merge.**
2. Onboard dobby via the runbook: mint cert, write `daemon.env`, enable the daemon, register dobby
   with a cap.
3. Verify a smoke-test session lands on dobby; then leave auto-dispatch on so rocky's load sheds to
   dobby.
4. Rollback at any point: disable/remove dobby in the Fleet UI → the registry is single-host →
   `FleetOrchestrator` degrades to rocky-only.

## Security (Principle IV)

No new trust surface beyond what sub-projects 2-3 already established: the daemon RPC is mTLS with
`CERT_REQUIRED` + the `edge` CN allowlist; a missing TLS file fails loud at startup (no insecure
fallback); the account-store mount is the same shared-credential surface already accepted for rocky.
dobby-as-runner adds no new *class* of exposure — dobby already holds the canonical credentials.

## Out of scope / deferred

- Repo-locality-aware ranking (decision 4) — future soft-preference enhancement.
- Balancing global/cross-cutting ops (automations, backlog, reauth) across hosts (decision 5).
- Live session **migration** between hosts (umbrella deferred).
- A third+ runner (the design generalizes; only dobby + rocky are provisioned — the runbook makes a
  third mechanical).
- Certificate rotation / ACME (operator-minted, manually renewed — umbrella deferred).

## Open questions

None blocking. Confirm during implementation: (a) exact `max_sessions` cap for dobby; (b) that no
new model/column is introduced (so the Postgres-suite obligation stays N/A); (c) whether the
manual-override `host` field should also be exposed on automation-created sessions or interactive
only (lean interactive-only first).
