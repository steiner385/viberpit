# Opt-in Trust of the Edge Host — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator opt in, with `VIBERPIT_RULES_TRUST_LOCAL_HOST=true`, to letting a browser on the
edge host write always-allow rules and permission-profile patterns.

**Architecture:** One new env-only setting, read in the one place that classifies human-only writes
(`identity.human_gate_via`). With the setting on, a request that would be classified `"local"` is
classified `"local-trusted"`, which `session_actions.require_human` admits. That `via` already
reaches the rule's `created_via` and the audit row, so no schema change is needed.

**Tech Stack:** Python 3.12, FastAPI, pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-10-14-rules-trust-local-host-design.md`

## Global Constraints

- Setting name: `VIBERPIT_RULES_TRUST_LOCAL_HOST`. Attribute `rules_trust_local_host`. Default `False`.
- Env only: no Settings UI control and no API write path.
- New `via` value: `LOCAL_TRUSTED_VIA = "local-trusted"`, defined in `server/auth/identity.py`.
- Token-borne callers (any `via` the middleware recorded) and `SYSTEM_IDENTITY` stay refused when the
  setting is on.
- The startup log line is INFO, never WARNING. WARNING auto-files an Error Triage issue.
- Environment for running tests (worktree has no venv): from the repo root,
  `env -u VIBERPIT_TAILNET_ALLOWLIST -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_DAEMON_ROLE -u CCSWITCH_DAEMON_ROLE VIBERPIT_DEV=1 /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …`.
  Also unset any `VIBERPIT_SESSIOND_*` and `CCSWITCH_SESSIOND_*` vars.
- Commit messages go in a file passed with `-F`, ending with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **The setting is on, but the caller holds a token** (the assistant `ccas1:`, external MCP, CLI,
   session). Expected: still refused. Pinned in Task 2 (`test_trust_local_host_never_admits_token_borne_callers`).
2. **The setting is on, and a remote device goes through serve.** Expected: `via` stays `None`, not
   `local-trusted`, so the audit trail doesn't mislabel phone writes. Pinned in Task 2 (parametrized unit test).
3. **The setting value is not exactly `true`** (`"1"`, `"yes"`, `"TRUE"`, or `"false"`). Expected: same
   parsing as the sibling booleans. Pinned in Task 1 (`test_rules_trust_local_host_parses_like_sibling_flags`).
4. **The legacy `decision: "always"` over WS from the host browser.** This is the exact path that produced
   the screenshot's error. Expected: the rule is created. Pinned in Task 2 (WS test).
5. **The setting is off, and the operator reads the 403.** Expected: the message names the setting and its
   cost. Pinned in Task 2 (`test_same_host_refusal_names_the_opt_in`).

---

### Task 1: The setting, and listing it on Settings › About

**Files:**
- Modify: `server/config.py` (pydantic `Settings`, next to `permission_rules_enabled` at about line 295;
  dataclass fallback, next to `permission_rules_enabled` at about line 628)
- Modify: `server/api/settings.py` (`_MANAGED`, after the `permission_rules_enabled` row)
- Test: `server/tests/test_rules_trust_local_host.py` (create)

**Interfaces:**
- Produces: `get_settings().rules_trust_local_host: bool`

- [ ] **Step 1: Write the failing tests**

```python
"""Opt-in trust of the edge host for human-only writes (spec 2026-10-14)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.config import get_settings


def test_rules_trust_local_host_defaults_off():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIBERPIT_RULES_TRUST_LOCAL_HOST", None)
        os.environ.pop("CCSWITCH_RULES_TRUST_LOCAL_HOST", None)
        assert get_settings().rules_trust_local_host is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("0", False),
])
def test_rules_trust_local_host_parses_like_sibling_flags(raw, expected):
    with patch.dict(os.environ, {"VIBERPIT_RULES_TRUST_LOCAL_HOST": raw,
                                 "VIBERPIT_PERMISSION_RULES_ENABLED": raw}):
        s = get_settings()
        assert s.rules_trust_local_host is expected
        assert s.rules_trust_local_host == s.permission_rules_enabled


def test_rules_trust_local_host_is_listed_on_about():
    from server.api.settings import _MANAGED
    row = next(r for r in _MANAGED if r[0] == "rules_trust_local_host")
    assert row[1] == "VIBERPIT_RULES_TRUST_LOCAL_HOST"
    assert "agents on the host" in row[2]
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `… -m pytest server/tests/test_rules_trust_local_host.py -v`
Expected: FAIL, with `AttributeError: 'Settings' object has no attribute 'rules_trust_local_host'` and `StopIteration`.

- [ ] **Step 3: Implement**

In `server/config.py`, pydantic branch, directly after the `permission_rules_enabled` field:

```python
        # Opt-in trust of the edge host for human-only writes (spec 2026-10-14). On = a
        # request from this host that human_gate_via cannot attest is admitted as the UI,
        # so a browser on the host can write always-allow rules and profile patterns. So
        # can any agent session there: the operator accepts that. Env only, never the UI.
        rules_trust_local_host: bool = Field(
            default=False, alias="VIBERPIT_RULES_TRUST_LOCAL_HOST")
```

Dataclass fallback, directly after its `permission_rules_enabled` field:

```python
        rules_trust_local_host: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("VIBERPIT_RULES_TRUST_LOCAL_HOST", "false").lower() in ("1", "true", "yes", "on")
        )
```

In `server/api/settings.py` `_MANAGED`, after the `permission_rules_enabled` row:

```python
    ("rules_trust_local_host", "VIBERPIT_RULES_TRUST_LOCAL_HOST", "Requests from the edge host itself may write always-allow rules and profile patterns (agents on the host can too)"),
```

- [ ] **Step 4: Run them and confirm they pass**

Run: `… -m pytest server/tests/test_rules_trust_local_host.py -v`
Expected: PASS.

Then run `python scripts/brand_ratchet.py` (the new name uses the `VIBERPIT_` prefix, so expect no change).

- [ ] **Step 5: Commit**

`git add server/config.py server/api/settings.py server/tests/test_rules_trust_local_host.py`, then
commit with message `feat(config): VIBERPIT_RULES_TRUST_LOCAL_HOST opt-in setting`.

---

### Task 2: The gate admits `local-trusted`, and the 403 names the opt-in

**Files:**
- Modify: `server/auth/identity.py` (constants near `LOCAL_PROCESS_VIA` at about line 237; `human_gate_via` at about line 408)
- Modify: `server/sessions/session_actions.py` (`require_human`, about lines 105-124)
- Test: `server/tests/test_rules_trust_local_host.py` (extend)

**Interfaces:**
- Consumes: `get_settings().rules_trust_local_host` (Task 1)
- Produces: `identity.LOCAL_TRUSTED_VIA = "local-trusted"`. `human_gate_via(conn) -> str | None` now
  returns one of `None`, a recorded `via`, `LOCAL_TRUSTED_VIA`, or `LOCAL_PROCESS_VIA`.

- [ ] **Step 1: Write the failing tests** (append to `server/tests/test_rules_trust_local_host.py`)

```python
import ipaddress
import json
import time

from server.auth import identity
from server.auth.identity import LOCAL_PROCESS_VIA, LOCAL_TRUSTED_VIA, human_gate_via
from server.sessions.session_actions import ActionError, require_human
from server.tests.test_local_identity_assertion import (
    _FRONT_ENDS, _OPERATOR_UID, _OWN, _PHONE, _REAL_ATTEST, _UIDS, _XFF, _conn,
    _local_client, _refused,
)
from server.tests.test_permission_rules_api import (
    _ENV, _H, S1, _always, _card, _make_client, _rules, _seed_sessions,
)
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401

_ON = {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "true"}


@pytest.fixture
def real_attestation(monkeypatch):
    # conftest stubs the attestation for every suite; these tests need the real one.
    monkeypatch.setattr(identity, "attested_front_end_hop", _REAL_ATTEST)
    monkeypatch.setattr(identity, "_front_end_headers", lambda: dict(_FRONT_ENDS))
    monkeypatch.setattr(identity, "_self_tailscale_ips",
                        lambda: frozenset({ipaddress.ip_address(_OWN)}))


@pytest.mark.parametrize("on,host,uids,headers,expected", [
    (True, "127.0.0.1", {_OPERATOR_UID}, {}, LOCAL_TRUSTED_VIA),       # host browser / curl
    (True, "127.0.0.1", {0}, {_XFF: _OWN}, LOCAL_TRUSTED_VIA),         # host browser via serve
    (True, "127.0.0.1", None, {}, LOCAL_TRUSTED_VIA),                  # /proc unreadable
    (True, "127.0.0.1", {0}, {_XFF: _PHONE}, None),                    # phone via serve: still human
    (True, _PHONE, set(), {}, None),                                   # remote peer
    (False, "127.0.0.1", {_OPERATOR_UID}, {}, LOCAL_PROCESS_VIA),
    (False, "127.0.0.1", {0}, {_XFF: _OWN}, LOCAL_PROCESS_VIA),
])
def test_human_gate_via_with_trust_local_host(real_attestation, on, host, uids, headers,
                                              expected):
    env = _ON if on else {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}
    with patch.dict(os.environ, env), patch(_UIDS, return_value=uids):
        assert human_gate_via(_conn(host, headers=headers)) == expected


def test_trust_local_host_never_admits_token_borne_callers(real_attestation):
    with patch.dict(os.environ, _ON), \
            patch(_UIDS, side_effect=AssertionError("must not read /proc")):
        for via in ("assistant", "mcp", "cli", "internal", "session"):
            assert human_gate_via(_conn("127.0.0.1", via=via)) == via
            with pytest.raises(ActionError) as exc:
                require_human("testuser@example.com", via)
            assert exc.value.code == "RULES_HUMAN_ONLY"


def test_require_human_admits_local_trusted_but_not_the_system_identity():
    from server.sessions.session_actions import SYSTEM_IDENTITY
    require_human("testuser@example.com", LOCAL_TRUSTED_VIA)   # no raise
    with pytest.raises(ActionError):
        require_human(SYSTEM_IDENTITY, LOCAL_TRUSTED_VIA)


def test_same_host_refusal_names_the_opt_in():
    with pytest.raises(ActionError) as exc:
        require_human("testuser@example.com", LOCAL_PROCESS_VIA)
    assert exc.value.http == 403
    assert "VIBERPIT_RULES_TRUST_LOCAL_HOST" in exc.value.message
    assert "agents on this host" in exc.value.message


def test_host_browser_writes_rules_and_profiles_when_trusted(real_attestation):
    client, fake = _make_client(_ON)
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm run build")]
    with patch.dict(os.environ, {**_ENV, **_ON}), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            r = _always(lc, S1, "p1", "Bash(npm test:*)", "session")
            assert r.status_code == 200, r.text
            rid = r.json()["rule"]["id"]
            assert r.json()["rule"]["createdVia"] == LOCAL_TRUSTED_VIA
            assert lc.post(f"/api/sessions/{S1}/permissions/p2",
                           json={"decision": "always"}, headers=_H).status_code == 200
            assert lc.patch(f"/api/permission-rules/{rid}",
                            json={"pattern": "Bash(npm run:*)"}, headers=_H).status_code == 200
            assert lc.post("/api/permission-profiles", headers=_H,
                           json={"name": "q", "patterns": ["Read"]}).status_code in (200, 201)
            assert lc.delete(f"/api/permission-rules/{rid}", headers=_H).status_code == 204
        audits = client.portal.call(_audit_vias)
    assert audits.get("permission_rule.create") == LOCAL_TRUSTED_VIA
    assert audits.get("permission_rule.revoke") == LOCAL_TRUSTED_VIA


def test_host_browser_still_refused_when_not_trusted(real_attestation):
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            _refused(_always(lc, S1, "p1", "Bash(npm test:*)", "session"))
        assert client.portal.call(_rules) == []


async def _audit_vias():
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent
    async with db_session() as db:
        rows = (await db.execute(select(AuditEvent.type, AuditEvent.via))).all()
    return {t: v for t, v in rows}


def test_ws_always_from_host_browser_uses_local_trusted(ws_env, real_attestation):  # noqa: F811
    _app, orch, client = ws_env([])
    session_id = client.portal.call(orch.create_session, "test-ws-always-trusted").id
    seen: list[tuple[str, str | None]] = []

    async def _capture(*_a, decision, via, **_k):
        seen.append((decision, via))
        return {}

    with patch.dict(os.environ, _ON), \
            patch("server.bridge.ws.session_actions.resolve_permission", _capture), \
            patch(_UIDS, return_value={_OPERATOR_UID}):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            ws.send_text(json.dumps({"type": "permissionResponse", "sessionId": session_id,
                                     "permId": "p-always", "decision": "always"}))
            deadline = time.monotonic() + 5.0
            while not seen and time.monotonic() < deadline:
                time.sleep(0.02)
    client.portal.call(orch.close_session, session_id)
    assert seen == [("always", LOCAL_TRUSTED_VIA)]
```

Note to the implementer: the WS test client's peer may not be an IP (`"testclient"`). If it isn't, the
real `human_gate_via` returns `None` through the non-IP shortcut and `seen` shows `None`. If that
happens, construct the WS client with `client=("127.0.0.1", 50000)` the way `_local_client` does for
HTTP. Don't loosen the assertion. If `_make_client(extra_env)` doesn't keep the env for the request
phase, the outer `patch.dict` above covers it, because `get_settings()` reads `os.environ` on every call.

- [ ] **Step 2: Run them and confirm they fail**

Run: `… -m pytest server/tests/test_rules_trust_local_host.py -v`
Expected: FAIL, with `ImportError: cannot import name 'LOCAL_TRUSTED_VIA'`.

- [ ] **Step 3: Implement**

`server/auth/identity.py`, directly after `LOCAL_PROCESS_VIA = "local"`:

```python
# ``via`` for the same local caller when the operator opted in to trusting this host
# (``VIBERPIT_RULES_TRUST_LOCAL_HOST``, spec 2026-10-14): ``require_human`` admits it, and
# it is what the rule's ``created_via`` and the audit row record, so writes made under
# the opt-in stay distinguishable from attested ones.
LOCAL_TRUSTED_VIA = "local-trusted"
```

Replace the body of `human_gate_via`, and add a sentence to its docstring:

```python
def human_gate_via(conn: Request | WebSocket) -> str | None:
    """``via`` for a human-only gate (``session_actions.require_human``): the attribution
    the middleware already recorded, else ``LOCAL_PROCESS_VIA`` unless
    ``attested_front_end_hop`` vouches that a remote person is behind this request
    (#2980). With ``rules_trust_local_host`` on, that unattested local caller is
    ``LOCAL_TRUSTED_VIA`` instead; a recorded (token-borne) ``via`` is never upgraded.
    Reads ``/proc``, so call it only on the rare writes it guards.
    """
    via = getattr(conn.state, "via", None)
    if via is not None:
        return via
    if attested_front_end_hop(conn):
        return None
    from server.config import get_settings  # noqa: PLC0415
    return LOCAL_TRUSTED_VIA if get_settings().rules_trust_local_host else LOCAL_PROCESS_VIA
```

(Check whether `server/auth/identity.py` already imports `get_settings` at module level. If it does,
use that import instead of the local one.)

`server/sessions/session_actions.py`: import `LOCAL_TRUSTED_VIA` alongside `LOCAL_PROCESS_VIA`, then
rewrite `require_human`:

```python
def require_human(identity: str, via: str | None) -> None:
    """Standing rules widen what a session may do unattended, so only a person at the UI
    may create, edit, revoke or preview one, or write permission-profile patterns: no
    token-borne caller (assistant, external MCP, CLI, internal, session), no process on
    this host asserting an identity header (``identity.LOCAL_PROCESS_VIA``, #2980; callers
    take ``via`` from ``identity.human_gate_via``) and not the system sentinel (spec §4).
    ``identity.LOCAL_TRUSTED_VIA`` is that local caller under the operator's opt-in
    (``VIBERPIT_RULES_TRUST_LOCAL_HOST``, spec 2026-10-14) and is admitted."""
    if identity == SYSTEM_IDENTITY:
        raise ActionError(
            RULE_HUMAN_ONLY_CODE,
            "Only a person in the UI can manage always-allow rules and permission profiles.",
            403)
    if via == LOCAL_TRUSTED_VIA:
        return
    if via == LOCAL_PROCESS_VIA:
        # A browser on the edge host itself lands here too: it is indistinguishable from
        # an agent session on that host, so say where the change CAN be made.
        raise ActionError(
            RULE_HUMAN_ONLY_CODE,
            "This request came from the ViberPit host itself, where an agent could send the "
            "same one. Change always-allow rules and permission profiles from a browser on "
            "another device on your tailnet (phone, laptop), or have the host's admin set "
            "VIBERPIT_RULES_TRUST_LOCAL_HOST=true in the ViberPit environment file, which "
            "lets agents on this host write them too.",
            403)
    if via is not None:
        raise ActionError(
            RULE_HUMAN_ONLY_CODE,
            "Only a person in the UI can manage always-allow rules and permission profiles.",
            403)
```

The `SYSTEM_IDENTITY` check moves first so `local-trusted` can never admit the system sentinel.

- [ ] **Step 4: Audit the downstream `via` consumers** (a spec requirement; the findings go in the PR body)

Run `grep -n "via" server/sessions/session_actions.py server/api/permission_rules.py server/api/permission_profiles.py server/api/sessions.py server/bridge/ws.py`.
For each consumer on the gated paths, confirm that `local-trusted` behaves like `None`:
- `resolve_permission`: `if via == "assistant"` → unaffected.
- `grant_always` → `store.add_rule(created_via=via)` and `record_audit(via=via)` → intended.
- `orch.resolve_permission(...)` takes no `via` → unaffected.

Any other consumer that branches on `via is None` must be fixed or recorded as harmless.

- [ ] **Step 5: Run the new and existing gate suites and confirm they pass**

Run: `… -m pytest server/tests/test_rules_trust_local_host.py server/tests/test_local_identity_assertion.py server/tests/test_session_actions.py server/tests/test_permission_rules_api.py server/tests/test_permission_profiles*.py -v 2>&1 | tee /tmp/trust-local-task2.log`
Expected: all PASS. Then run the mutation checks by hand, reverting each one afterward:
1. Delete the `if via == LOCAL_TRUSTED_VIA: return` lines. `test_host_browser_writes_rules_and_profiles_when_trusted` must fail.
2. Change `human_gate_via` so it returns `LOCAL_TRUSTED_VIA` *before* the recorded-`via` check. `test_trust_local_host_never_admits_token_borne_callers` must fail.
3. Move the `SYSTEM_IDENTITY` check below the `LOCAL_TRUSTED_VIA` return. `test_require_human_admits_local_trusted_but_not_the_system_identity` must fail.

- [ ] **Step 6: Commit**

`git add server/auth/identity.py server/sessions/session_actions.py server/tests/test_rules_trust_local_host.py`, then
commit with message `feat(permissions): admit same-host rule writes under VIBERPIT_RULES_TRUST_LOCAL_HOST`.

---

### Task 3: Startup INFO line and docs

**Files:**
- Modify: `server/app.py` (`_lifespan`, right after the F1 identity-header check at about line 197)
- Modify: `docs/security/2026-10-11-local-identity-assertion.md` (the decision table at lines 85-96; the residual-risk bullet at lines 128-132)
- Modify: `deploy/README.md` (the "Edge unit" section, after line 107)
- Test: `server/tests/test_rules_trust_local_host.py` (extend)

**Interfaces:**
- Consumes: `get_settings().rules_trust_local_host` (Task 1)
- Produces: `server.app._log_rules_trust_local_host() -> None`

- [ ] **Step 1: Write the failing test**

```python
import logging


def test_startup_logs_trust_local_host_at_info_only_when_on(caplog):
    from server.app import _log_rules_trust_local_host
    with caplog.at_level(logging.INFO, logger="server.auth"):
        with patch.dict(os.environ, {"VIBERPIT_RULES_TRUST_LOCAL_HOST": "false"}):
            _log_rules_trust_local_host()
        assert not caplog.records
        with patch.dict(os.environ, _ON):
            _log_rules_trust_local_host()
    [rec] = caplog.records
    assert rec.levelno == logging.INFO
    assert "VIBERPIT_RULES_TRUST_LOCAL_HOST" in rec.getMessage()
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `… -m pytest server/tests/test_rules_trust_local_host.py::test_startup_logs_trust_local_host_at_info_only_when_on -v`
Expected: FAIL, with `ImportError`.

- [ ] **Step 3: Implement**

In `server/app.py`, at module level above `_lifespan`:

```python
def _log_rules_trust_local_host() -> None:
    """One INFO line when the operator trusts this host for rule writes (spec 2026-10-14).
    INFO, not WARNING: a WARNING would auto-file an Error Triage issue every restart."""
    if get_settings().rules_trust_local_host:
        _logging.getLogger("server.auth").info(
            "VIBERPIT_RULES_TRUST_LOCAL_HOST is on: requests from this host, including agent "
            "sessions, may write always-allow rules and permission-profile patterns")
```

(Use whatever name `server/app.py` already imports for `logging` and `get_settings`. The file uses
`_logging` at line 458; check before relying on that name.) Call `_log_rules_trust_local_host()` in
`_lifespan`, immediately after the F1 identity-header check block.

- [ ] **Step 4: Run it and confirm it passes**

Same command as Step 2. Expected: PASS.

- [ ] **Step 5: Update the threat note**

In `docs/security/2026-10-11-local-identity-assertion.md`:

- Add this row to the decision table, after the header row:

```markdown
| any row below that yields `"local"`, with `VIBERPIT_RULES_TRUST_LOCAL_HOST=true` | `"local-trusted"`: admitted, recorded as such (spec 2026-10-14) |
```

- Replace the residual-risk bullet that begins "**A browser on the edge host itself cannot write rules or profile patterns.**" with:

```markdown
- **A browser on the edge host itself cannot write rules or profile patterns by default.** At the
  network level it is the same as an agent session on that host: same user, same source address,
  same front-end. Operators who use ViberPit mainly from the host can opt in with
  `VIBERPIT_RULES_TRUST_LOCAL_HOST=true` (spec `docs/superpowers/specs/2026-10-14-rules-trust-local-host-design.md`).
  The cost is explicit: every process on the host running as the operator, agent sessions
  included, can then write rules and profile patterns. Token-borne callers stay refused. Writes
  admitted this way carry `via`/`created_via` = `local-trusted`. The setting lives only in the
  root-owned env file, so a session cannot turn it on. A passkey step-up (#1410) remains the
  route to an agent-proof answer.
```

- [ ] **Step 6: Update `deploy/README.md`**

After the block ending `# want 1` in "Edge unit: `--no-proxy-headers` (#2980)", add:

````markdown
### Writing rules from the host's own browser (opt-in)

By default, a browser on the edge host gets `RULES_HUMAN_ONLY` when it writes always-allow rules or
permission-profile patterns, because it looks the same as an agent session there. If you mainly use
ViberPit from the host and accept that agent sessions on it could then write rules too, opt in:

```bash
echo 'VIBERPIT_RULES_TRUST_LOCAL_HOST=true' | sudo tee -a /etc/viberpit/viberpit.env
sudo systemctl restart viberpit.service   # the edge only; sessions on the daemon keep running
```

Confirm it on Settings › About, or with `journalctl -u viberpit.service | grep RULES_TRUST_LOCAL_HOST`.
Keep `/etc/viberpit/viberpit.env` `root:root 0600`. If a session can write that file, it can turn
this on. Rules written under the opt-in show `createdVia: local-trusted`.
````

- [ ] **Step 7: Commit**

`git add server/app.py server/tests/test_rules_trust_local_host.py docs/security/2026-10-11-local-identity-assertion.md deploy/README.md`, then
commit with message `feat(edge): log the local-host trust opt-in at startup; document it`.

---

### Task 4: Full gate and PR

- [ ] **Step 1:** Run all four `lint-and-test` steps (each piped through `tee` to a log under `/tmp/`):
  `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, and
  `pytest server/tests/ -n auto --dist loadfile --timeout=300` (run this last one in the background).
- [ ] **Step 2:** Check for in-flight PRs touching `server/auth/identity.py`, `server/sessions/session_actions.py`,
  or `deploy/README.md` (the GraphQL query in core.md). Rebase if needed.
- [ ] **Step 3:** Push and open the PR. The body covers the summary, the Task 2 Step 4 consumer audit,
  `Refs #3065`, `Refs #2980`, and the rollout step for dobby. Mark it ready, then run `gh pr merge <#> --squash --auto`.
