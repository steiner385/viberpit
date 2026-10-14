# Opt-in trust of the edge host for human-only writes

Status: approved 2026-10-14. Refs #2980 (the same-host gate), #3065 (its refusals), #1410
(strong authentication; out of scope here).

## Problem

`identity.human_gate_via` refuses every always-allow rule write and permission-profile pattern
write that it cannot attest as coming from another tailnet device
(`docs/security/2026-10-11-local-identity-assertion.md`). A browser on the edge host is
indistinguishable from an agent session there, so it is refused too.

Most operators use ViberPit mainly from the browser on the host that runs it (dobby, here). For
them, the gate locks a core feature behind a second device. The 403 tells them to use a phone or
laptop, and that is not an acceptable answer for a primary workflow.

## Decision

Add an operator opt-in, **`VIBERPIT_RULES_TRUST_LOCAL_HOST`** (bool, default `false`). When it is
on, a request the gate would classify as `"local"` is admitted as a person at the UI. The
operator accepts that any process on the host running as their user, including an agent session,
can then write standing rules and profile patterns.

Considered and not chosen:

- **WebAuthn step-up** (a passkey with user verification) is agent-proof and would advance
  #1410. The owner chose the simpler opt-in for now. It stays the path to a stronger guarantee.
- **Trusting only requests that pass through a front-end** (serve or nginx) stamped with this
  host's address. An agent reaches the tailnet URL as easily as a browser does, so this would add
  code without adding protection.

## Design

### Setting

- Expose `rules_trust_local_host` on both settings classes in `server/config.py` as a read-only
  property, not a field. Both properties call one module-level helper that parses
  `os.environ["VIBERPIT_RULES_TRUST_LOCAL_HOST"]` with the sibling flags' truthy set
  (`1`/`true`/`yes`/`on`, case-insensitive), default `False`. `get_settings()` mirrors the legacy
  prefix first, as for every setting.
- **Why not a field:** pydantic `Settings` also reads `.env` from the working directory, on every
  `get_settings()` call. The edge's working directory is the deploy checkout, which the operator's
  user, and so every agent session, can write. A field would let a session append the flag there
  and have its next rule write admitted, with no restart. The property reads only the process
  environment, which systemd sets at start and nothing can change afterwards.
- **Environment only.** No Settings UI control and no API write path. An agent can call the API
  but cannot edit the edge's env file: on dobby, `/etc/viberpit/viberpit.env` is `root:root 0600`.
  The deploy docs say the file must stay root-owned.
- Add it to the read-only Settings › About list (`_MANAGED` in `server/api/settings.py`) with
  the description "Requests from the edge host itself may write always-allow rules and profile
  patterns (agents on the host can too)".

### Gate

- `server/auth/identity.py`: add `LOCAL_TRUSTED_VIA = "local-trusted"` beside `LOCAL_PROCESS_VIA`.
- `human_gate_via(conn)`:
  1. A `via` the middleware already recorded (a token-borne caller) is returned unchanged. The
     setting never admits token-borne callers.
  2. An attested front-end hop returns `None`, as today.
  3. Otherwise it returns `LOCAL_TRUSTED_VIA` when the setting is on, else `LOCAL_PROCESS_VIA`.
- `server/sessions/session_actions.py::require_human`: admit `via == LOCAL_TRUSTED_VIA` (return,
  no error). Every other path is unchanged: `LOCAL_PROCESS_VIA` gets the same-host 403, and any
  other non-`None` `via` or the system identity gets the generic 403.
- No route changes. The existing callers of `human_gate_via` cover every gated write:
  `permission_rules.py`, `permission_profiles.py`, `sessions.py` (`decision: "always"`), and
  `bridge/ws.py` (`permissionResponse` always).
- **Audit trail with no schema change.** The returned `via` already flows into `record_audit(...,
  via=via)` and into the rule's `created_via`. So rules and audit rows written under the setting
  carry `local-trusted`.
- Implementation must check every downstream consumer of `via` along these paths. Anything that
  branches on `via is None` to mean "human" must treat `LOCAL_TRUSTED_VIA` as human too, or a
  call site must be shown not to care. Record the findings in the PR.

### Visibility

- The same-host 403 message (`RULES_HUMAN_ONLY`, `LOCAL_PROCESS_VIA` branch) adds the
  alternative. Suggested text: "…from a browser on another device on your tailnet (phone,
  laptop), or have the host's admin set VIBERPIT_RULES_TRUST_LOCAL_HOST=true in the ViberPit
  environment file, which lets agents on this host write them too."
- At edge startup, when the setting is on, log one line at **INFO**, not WARNING. A WARNING
  auto-files an Error Triage issue on every restart.

### Docs

- Threat note `docs/security/2026-10-11-local-identity-assertion.md`: the residual-risk bullet
  "A browser on the edge host itself cannot write rules…" now names the opt-in and states its
  cost. Add a row to the decision table for the setting.
- `deploy/README.md`, under "Edge unit": how to enable the setting (edit the root-owned env file
  with sudo, then `systemctl restart viberpit.service`, which restarts the edge only, not the
  session daemon), how to confirm it on Settings › About, and the warning that the env file must
  stay root-owned.

## Testing

Add to `server/tests/test_local_identity_assertion.py` and `server/tests/test_session_actions.py`:

- Setting on: a uid-1000 local socket is admitted on every gated route. Use the existing
  route-parametrized "refuses a local socket" cases, flipped.
- Setting on: token-borne callers (`via` set by the middleware) and `SYSTEM_IDENTITY` are still
  refused.
- Setting on: an attested remote hop still yields `None`, not `local-trusted`.
- Setting off: behavior is unchanged. The 403 text names `VIBERPIT_RULES_TRUST_LOCAL_HOST`.
- A rule created under the setting has `created_via == "local-trusted"`, and so does its audit
  row.
- The setting appears in About and defaults to false.
- Mutation check: removing the `LOCAL_TRUSTED_VIA` admission in `require_human` fails a test.
  Making the setting admit token-borne callers also fails a test.

## Rollout

After the merge deploys, the operator adds `VIBERPIT_RULES_TRUST_LOCAL_HOST=true` to
`/etc/viberpit/viberpit.env` and restarts `viberpit.service`. No migration.
