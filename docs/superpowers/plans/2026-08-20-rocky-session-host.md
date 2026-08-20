# Rocky as a Session Host — Implementation Plan (sub-project 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the ccswitch session daemon on **rocky**, reached by dobby's edge over mTLS, so
`claude` sessions execute on rocky's CPU/RAM while dobby keeps the UI, the database, and the
canonical credentials.

**Architecture:** rocky gets a ccswitch checkout, a venv (which supplies `claude` via the bundled
`claude_agent_sdk`), a systemd unit, and mTLS material. The daemon binds
`https://rocky.lan:9443` instead of a Unix socket; dobby's edge points at that endpoint. Both the
edge and rocky's daemon talk to the **same Postgres on dobby**, which must first be opened to the
LAN. Credentials are not copied to rocky — one account directory is mounted read-write from dobby,
a minimal preview of sub-project 3.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + asyncpg, uvicorn, systemd, OpenSSL, sshfs, PostgreSQL 16.

**Spec:** `docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md` (sub-project 2).

## Global Constraints

- **Never break dobby.** dobby is live on Postgres serving real sessions. Every dobby-side change
  (Postgres networking, edge endpoint) must be revertible in one step, and the edge stays on its
  Unix socket until the final task.
- **No credentials copied to rocky.** Decision 4 supersedes the just-in-time-push model: dobby holds
  the canonical account directories; rocky mounts what it needs.
- **mTLS is not optional.** An `https://` endpoint with missing TLS files must fail loud at startup.
  Never fall back to plaintext.
- **The CA private key does not live on either host** where practical — mint certs, then move the CA
  key off to `/mnt/hdd-storage/ccswitch-ca/` (root-only), not into the repo, and never commit it.
- **Run commands in the FOREGROUND, bounded.** Do not background a long run and wait on it.
- **The controller supplies secrets.** `<pw>` (the Postgres password) and `CCSWITCH_MASTER_KEY`
  are NOT to be discovered, echoed, or written into reports by an implementer — they are handed
  in at dispatch or written by the controller directly, as in sub-project 1b.
- **Test command:**
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_SESSIOND_SOCKET=/nonexistent/ccswitch-test.sock .venv/bin/python -m pytest <target> -q`

## Verified facts (probed 2026-08-20)

| | dobby | rocky |
|---|---|---|
| Cores / RAM | 16 / 19 GB | 8 / 31 GB |
| Python | 3.12 | **3.12.3 — matches** |
| node / npm | yes | v20.20.2 / 10.8.2 |
| Xvfb / x11vnc | yes | **already installed** (x11vnc 0.9.16) |
| `psql` client | 16.14 | **16.14 — matches** |
| `claude` binary | from `claude_agent_sdk/_bundled/claude` in the venv | comes with the venv, no separate install |
| `uv` | yes | **missing** — use `python3 -m venv` + `pip` |
| ccswitch repo / `/etc/ccswitch` | yes | **absent** |
| Reachable as | — | `rocky.lan`, `192.168.243.246` |
| Passwordless SSH `tony@rocky` | **working** | — |

**Postgres currently rejects rocky:** `listen_addresses = localhost`, and `pg_hba.conf` allows only
`127.0.0.1/32` and `::1/128`. This is new since the spec was written (1b cut over today) and is a
hard prerequisite.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `scripts/ccswitch-mint-mtls.sh` | CA + server + client certs with correct SANs | Create |
| `deploy/ccswitch-daemon-remote.service` | rocky's daemon unit (https endpoint, rocky paths) | Create |
| `docs/runbooks/rocky-session-host.md` | provisioning + rollback runbook | Create |
| `server/deploy/steps.py` | daemon restart must reach rocky over SSH | Modify |
| `server/tests/test_mtls_endpoint.py` | endpoint/TLS wiring coverage | Create |

---

### Task 1: Open Postgres to the LAN (dobby-side, revertible)

rocky's daemon cannot reach the database at all until this lands. Do it first and prove it from
rocky before building anything on top.

**Files:** none in-repo — `/etc/postgresql/16/main/postgresql.conf`, `pg_hba.conf`.

- [ ] **Step 1: Record the current values so the change is revertible**

```bash
sudo -u postgres psql -tAc "SHOW listen_addresses;"   # expect: localhost
sudo cp /etc/postgresql/16/main/postgresql.conf /etc/postgresql/16/main/postgresql.conf.pre-lan
sudo cp /etc/postgresql/16/main/pg_hba.conf      /etc/postgresql/16/main/pg_hba.conf.pre-lan
```

- [ ] **Step 2: Bind to the LAN address only — not `*`**

Edit `/etc/postgresql/16/main/postgresql.conf`:

```
listen_addresses = 'localhost,192.168.243.192.168.243.183'
```

Determine dobby's LAN IP first with `ip -4 addr show scope global`. dobby currently answers on **192.168.243.183** and
192.168.243.204 (two LAN interfaces) plus tailscale 100.102.11.121 — bind ONLY the address rocky
will actually use, never `*`, so Postgres is not exposed on an interface we did not intend.

- [ ] **Step 3: Allow ONLY rocky, ONLY the ccswitch database, with scram auth**

Append to `/etc/postgresql/16/main/pg_hba.conf` — above any broader rule:

```
# rocky session host — ccswitch daemon only
host    ccswitch        ccswitch        192.168.243.246/32       scram-sha-256
```

A `/32` host rule and a named database/role: do not widen to a subnet, `all`, or `trust`.

- [ ] **Step 4: Reload (not restart — no need to drop live connections)**

```bash
sudo systemctl reload postgresql
sudo -u postgres psql -tAc "SHOW listen_addresses;"
```

- [ ] **Step 5: Prove it from rocky, and prove it is still locked down**

```bash
# from rocky — must SUCCEED
ssh tony@rocky 'PGPASSWORD=<pw> psql -h 192.168.243.183 -U ccswitch -d ccswitch -tAc "SELECT current_database();"'
# from rocky — must FAIL (wrong database is not permitted by the /32 rule above)
ssh tony@rocky 'PGPASSWORD=<pw> psql -h 192.168.243.183 -U ccswitch -d postgres -tAc "SELECT 1;"'
```
Expected: `ccswitch` on the first, an authentication/permission error on the second. If the second
succeeds, the `pg_hba` rule is too broad — fix it before continuing.

- [ ] **Step 6: Confirm dobby is unharmed**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/health   # 200
journalctl -u ccswitch-daemon.service --since "2 minutes ago" --no-pager | grep -ci "error"
```

---

### Task 2: Provision rocky (repo, venv, directories)

**Files:** none in-repo. All work happens on rocky over SSH.

- [ ] **Step 1: Clone the repo to a path that mirrors dobby's layout**

```bash
ssh tony@rocky 'mkdir -p ~/GitHub && git clone https://github.com/steiner385/ccswitch.git ~/GitHub/ccswitch && git -C ~/GitHub/ccswitch log --oneline -1'
```

- [ ] **Step 2: Build the venv — this is where `claude` comes from**

rocky has no `uv`; use stdlib venv + pip. The `claude-agent-sdk` download is ~70 MB, so allow time.

```bash
ssh tony@rocky 'cd ~/GitHub/ccswitch && python3 -m venv .venv && .venv/bin/python -m pip install -q --upgrade pip && .venv/bin/pip install -e ".[server]"'
```

- [ ] **Step 3: Verify the bundled `claude` and the Postgres driver both exist**

```bash
ssh tony@rocky 'ls -la ~/GitHub/ccswitch/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude && ~/GitHub/ccswitch/.venv/bin/python -c "import asyncpg, sqlalchemy, uvicorn; print(\"deps ok\", asyncpg.__version__)"'
```
Expected: the `claude` file listed, and `deps ok 0.31.x`. If `claude` is absent the daemon cannot
launch sessions — stop and resolve before continuing.

- [ ] **Step 4: Create the runtime directories**

```bash
ssh tony@rocky 'sudo mkdir -p /etc/ccswitch /var/lib/ccswitch /run/ccswitch && sudo chown tony:tony /var/lib/ccswitch /run/ccswitch && sudo chmod 700 /etc/ccswitch'
```

- [ ] **Step 5: Confirm rocky can reach dobby's Postgres from inside the venv**

```bash
ssh tony@rocky 'cd ~/GitHub/ccswitch && .venv/bin/python -c "
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect(host=\"192.168.243.183\", user=\"ccswitch\", password=\"<pw>\", database=\"ccswitch\")
    print(\"pg ok\", await c.fetchval(\"SELECT COUNT(*) FROM sessions\"))
    await c.close()
asyncio.run(m())"'
```
Expected: `pg ok 151` (or the current count). This proves Task 1 end to end from the real client.

---

### Task 3: Mint mTLS material

**Files:** Create `scripts/ccswitch-mint-mtls.sh`

**Interfaces:** Produces `ca.pem`, `ca.key`, `server.pem`/`server.key` (for rocky), and
`client.pem`/`client.key` (for dobby's edge).

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Mint the private CA + server/client certs for the ccswitch cross-host daemon channel.
#
# The server cert's SAN MUST match how the EDGE reaches the daemon. ccswitch's own
# quickstart flags hostname mismatch as the number-one footgun here: a mismatch fails
# with "certificate does not match the server name" and looks like a TLS bug.
#
#   bash scripts/ccswitch-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>
set -euo pipefail

OUT="${1:?usage: ccswitch-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>}"
DNS="${2:?daemon dns name, e.g. rocky.lan}"
IP="${3:?daemon ip, e.g. 192.168.243.246}"
mkdir -p "$OUT"; cd "$OUT"; umask 077

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem \
  -subj "/CN=ccswitch-ca" -days 3650 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=sessiond" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 825 \
  -extfile <(printf "subjectAltName=DNS:%s,IP:%s" "$DNS" "$IP") 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr \
  -subj "/CN=edge" 2>/dev/null
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out client.pem -days 825 2>/dev/null

rm -f server.csr client.csr
chmod 600 ./*.key
echo "minted in $OUT: ca.pem server.pem/.key (SAN DNS:$DNS,IP:$IP) client.pem/.key (CN=edge)"
```

- [ ] **Step 2: Mint and verify the SAN is actually present**

```bash
bash scripts/ccswitch-mint-mtls.sh /tmp/ccswitch-mtls rocky.lan 192.168.243.246
openssl x509 -in /tmp/ccswitch-mtls/server.pem -noout -text | grep -A1 "Subject Alternative Name"
openssl x509 -in /tmp/ccswitch-mtls/client.pem -noout -subject
openssl verify -CAfile /tmp/ccswitch-mtls/ca.pem /tmp/ccswitch-mtls/server.pem /tmp/ccswitch-mtls/client.pem
```
Expected: SAN shows `DNS:rocky.lan, IP Address:192.168.243.246`; client subject `CN=edge`; both
certs report `OK` against the CA. A missing SAN here is the failure the script's header warns about.

- [ ] **Step 3: Distribute — server material to rocky, client material to dobby**

```bash
ssh tony@rocky 'sudo mkdir -p /etc/ccswitch/tls && sudo chown tony:tony /etc/ccswitch/tls && chmod 700 /etc/ccswitch/tls'
scp /tmp/ccswitch-mtls/{ca.pem,server.pem,server.key} tony@rocky:/etc/ccswitch/tls/
sudo mkdir -p /etc/ccswitch/tls && sudo cp /tmp/ccswitch-mtls/{ca.pem,client.pem,client.key} /etc/ccswitch/tls/
sudo chmod 600 /etc/ccswitch/tls/*.key
```

- [ ] **Step 4: Move the CA key off both hosts**

```bash
sudo mkdir -p /mnt/hdd-storage/ccswitch-ca && sudo chmod 700 /mnt/hdd-storage/ccswitch-ca
sudo mv /tmp/ccswitch-mtls/ca.key /mnt/hdd-storage/ccswitch-ca/
sudo rm -rf /tmp/ccswitch-mtls
ls -la /etc/ccswitch/tls/   # ca.key must NOT be here
```
Compromise of the CA key compromises the whole channel; it is only needed again to mint new certs.

- [ ] **Step 5: Commit the script (never the certs)**

```bash
git status --short   # confirm no .pem/.key files are staged
git add scripts/ccswitch-mint-mtls.sh
git commit -m "chore(mtls): add cert-minting script for the cross-host daemon channel"
```

---

### Task 4: Run the daemon on rocky over mTLS

**Files:** Create `deploy/ccswitch-daemon-remote.service`

- [ ] **Step 1: Write rocky's env file**

On rocky, `/etc/ccswitch/ccswitch.env` (root-only, 0600). It needs the Postgres URL, the master key
(to decrypt account credentials), the mTLS material, and the https endpoint:

```
HOME=/home/tony
PATH=/home/tony/GitHub/ccswitch/.venv/bin:/usr/local/bin:/usr/bin:/bin
CCSWITCH_DB_URL=postgresql+asyncpg://ccswitch:<pw>@192.168.243.183:5432/ccswitch
CCSWITCH_MASTER_KEY=<same value as dobby>
CCSWITCH_SESSIOND_ENDPOINT=https://rocky.lan:9443
CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem
CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge
CCSWITCH_ENABLE_XVFB=true
```

`CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge` narrows trust from "any CA-signed client" to the one
client CN we minted — the v2 hardening that has since shipped in `server/ipc/tls.py`.

- [ ] **Step 2: Write the unit**

`deploy/ccswitch-daemon-remote.service` — same shape as `deploy/ccswitch-daemon.service` but with
rocky's paths. Keep `Restart=always`, `TimeoutStopSec=90`, `KillMode=control-group`, `TasksMax=infinity`.
Set `MemoryHigh=24G` / `MemoryMax=28G` — rocky has 31 GB, versus dobby's 10 G/12 G on a 19 GB host,
and memory headroom is the main reason for offloading.

```
[Unit]
Description=ccswitch session daemon (remote host) — owns live claude sessions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tony
RuntimeDirectory=ccswitch
RuntimeDirectoryPreserve=yes
StateDirectory=ccswitch
WorkingDirectory=/home/tony/GitHub/ccswitch
EnvironmentFile=/etc/ccswitch/ccswitch.env
ExecStart=/home/tony/GitHub/ccswitch/.venv/bin/python -m server.ipc
Restart=always
RestartSec=3
TimeoutStopSec=90
KillMode=control-group
MemoryHigh=24G
MemoryMax=28G
TasksMax=infinity
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Install and start it on rocky**

```bash
scp deploy/ccswitch-daemon-remote.service tony@rocky:/tmp/
ssh tony@rocky 'sudo install -m644 /tmp/ccswitch-daemon-remote.service /etc/systemd/system/ccswitch-daemon.service && sudo systemctl daemon-reload && sudo systemctl enable --now ccswitch-daemon.service'
ssh tony@rocky 'systemctl is-active ccswitch-daemon.service; journalctl -u ccswitch-daemon.service -n 30 --no-pager'
```

- [ ] **Step 4: Prove mTLS is actually enforced — the security check**

From dobby:

```bash
# WITHOUT a client cert — must FAIL the handshake
curl -sS --cacert /etc/ccswitch/tls/ca.pem https://rocky.lan:9443/info ; echo "exit=$?"
# WITH the client cert — must return the daemon's /info JSON
curl -sS --cacert /etc/ccswitch/tls/ca.pem \
     --cert /etc/ccswitch/tls/client.pem --key /etc/ccswitch/tls/client.key \
     https://rocky.lan:9443/info; echo
```
Expected: the first fails with a TLS alert (non-zero exit); the second returns JSON containing
`protocol`. **If the first SUCCEEDS, mTLS is not being enforced — stop and fix before going further.**

- [ ] **Step 5: Confirm the protocol version matches dobby's build**

The edge refuses an incompatible daemon. Compare `protocol` from the `/info` above against
`python -c "from server.ipc import protocol; print(protocol.PROTOCOL_VERSION)"` on dobby. They must
be equal; if not, rocky's checkout is at a different commit — fix by pulling rocky to the same SHA.

---

### Task 5: Mount one account directory and run a real session

This is the end-to-end proof, and a minimal preview of sub-project 3's shared mount. **No credentials
are copied** — dobby stays canonical.

- [ ] **Step 1: Mount the account directory from dobby onto rocky over sshfs**

```bash
ssh tony@rocky 'sudo apt-get install -y sshfs' 2>&1 | tail -2
ssh tony@rocky 'mkdir -p ~/.claude-accounts/cfg'
ssh tony@rocky 'sshfs -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 tony@192.168.243.183:/home/tony/.claude-accounts/cfg/<account> ~/.claude-accounts/cfg/<account>'
ssh tony@rocky 'ls -la ~/.claude-accounts/cfg/<account>/.credentials.json'
```
Pick ONE account for this test. The mount is deliberately manual here; sub-project 3 makes it a
hardened systemd mount unit with retry.

- [ ] **Step 2: Point dobby's edge at rocky's daemon**

Add to dobby's `/etc/ccswitch/ccswitch.env` (keep the old socket line commented, not deleted, so
rollback is one edit):

```
CCSWITCH_SESSIOND_ENDPOINT=https://rocky.lan:9443
CCSWITCH_SESSIOND_CLIENT_CERT=/etc/ccswitch/tls/client.pem
CCSWITCH_SESSIOND_CLIENT_KEY=/etc/ccswitch/tls/client.key
CCSWITCH_SESSIOND_CLIENT_CA=/etc/ccswitch/tls/ca.pem
# CCSWITCH_SESSIOND_SOCKET=/run/ccswitch/sessiond.sock
```

Then stop dobby's LOCAL daemon (it must not compete) and restart the edge:

```bash
sudo systemctl stop ccswitch-daemon.service && sudo systemctl disable ccswitch-daemon.service
sudo systemctl restart ccswitch.service
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/health
```

- [ ] **Step 3: Create a session and confirm it runs ON ROCKY**

Through the UI or API, create a session on the mounted account. Then prove where it executes:

```bash
ssh tony@rocky 'pgrep -af "claude_agent_sdk/_bundled/claude" | head -3'
pgrep -af "claude_agent_sdk/_bundled/claude" | head -3   # on dobby — expect NOTHING
```
Expected: a `claude` process on **rocky**, none on dobby. That is the whole point of this
sub-project; if claude is running on dobby, the edge is still talking to a local daemon.

- [ ] **Step 4: Exercise the session and check both logs**

Send a prompt; confirm output streams back to the browser. Then:

```bash
ssh tony@rocky 'journalctl -u ccswitch-daemon.service --since "5 minutes ago" --no-pager | grep -ciE "error|traceback"'
journalctl -u ccswitch.service --since "5 minutes ago" --no-pager | grep -ciE "error|traceback|handshake"
```

- [ ] **Step 5: Rollback rehearsal — prove you can get back**

Before declaring success, rehearse the reversal: re-enable dobby's daemon, restore the socket line,
restart the edge, confirm health 200 and a session starts locally. Then redo Step 2 to return to
rocky. A cross-host setup you cannot reverse under pressure is not finished.

---

### Task 6: Teach the deploy to restart rocky's daemon

`server.deploy` restarts the daemon with a local `systemctl`. With the daemon on rocky, every deploy
would restart the wrong (now-disabled) unit and silently leave rocky running stale code — exactly the
class of failure that caused today's cutover rollback.

**Files:** Modify `server/deploy/steps.py`; Test `server/tests/test_deploy_remote_restart.py`

- [ ] **Step 1: Write the failing test**

The real function is `restart_units(*, daemon_changed, daemon_enabled, run=subprocess.run)` at
`server/deploy/steps.py:182`. It takes an injectable `run`, so the test needs no monkeypatching of
module globals — pass a recorder.

**Note the existing `daemon_enabled` guard.** Today the daemon restart is skipped entirely when the
unit is disabled — and Task 5 *disables* dobby's daemon. So without this change a deploy silently
restarts nothing on rocky and leaves it on stale code. That is precisely the stale-code/schema
mismatch that forced today's Postgres cutover rollback, so it is not hypothetical.

```python
import os
import pytest
from server.deploy import steps


def _recorder(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        class R:  # minimal stand-in for CompletedProcess
            returncode = 0
            stdout = b""
            stderr = b""
        return R()
    return run


def test_remote_daemon_restarts_over_ssh(monkeypatch):
    """With a remote daemon host configured, the daemon restart must go over SSH."""
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=False,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("ssh tony@rocky") for c in joined), (
        f"expected an ssh daemon restart, got {joined}")
    assert any("ccswitch-daemon.service" in c for c in joined)
    assert "ccswitch-daemon.service" in units
    # the edge still restarts locally, and still AFTER the daemon (order is load-bearing, #190)
    assert joined[-1] == "sudo systemctl restart ccswitch.service"


def test_local_behavior_unchanged_without_the_env_var(monkeypatch):
    """Single-host deployments must behave exactly as before."""
    monkeypatch.delenv("CCSWITCH_DAEMON_SSH_HOST", raising=False)
    calls: list[list[str]] = []
    units = steps.restart_units(daemon_changed=True, daemon_enabled=True,
                                run=_recorder(calls))
    joined = [" ".join(c) for c in calls]
    assert joined == ["sudo systemctl restart ccswitch-daemon.service",
                      "sudo systemctl restart ccswitch.service"]
    assert units == ["ccswitch-daemon.service", "ccswitch.service"]
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_deploy_remote_restart.py -q`
Expected: FAIL — the restart uses local `systemctl`.

- [ ] **Step 3: Implement**

In `restart_units`, branch on `CCSWITCH_DAEMON_SSH_HOST`:

- **Set** → restart over SSH (`["ssh", host, "sudo", "systemctl", "restart", "ccswitch-daemon.service"]`)
  and treat the remote daemon as enabled — do NOT let the local `daemon_enabled=False` guard skip
  it, since dobby's unit is deliberately disabled once the daemon lives on rocky.
- **Unset** → today's behavior byte-for-byte, including the `daemon_changed and daemon_enabled`
  guard, so single-host deployments are untouched.

Preserve the daemon-before-edge ordering either way — the docstring notes it is load-bearing for
the browser reconnect-flush race (#190). Add a comment explaining that skipping the remote restart
would leave rocky on stale code.

- [ ] **Step 4: Verify**

```
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_deploy_remote_restart.py server/tests/ -q -k deploy
.venv/bin/python -m mypy server/ --ignore-missing-imports
.venv/bin/python -m ruff check server/ scripts/
```

- [ ] **Step 5: Commit**

```bash
git add server/deploy/steps.py server/tests/test_deploy_remote_restart.py
git commit -m "feat(deploy): restart the session daemon over SSH when it runs on a remote host"
```

---

### Task 7: Runbook

**Files:** Create `docs/runbooks/rocky-session-host.md`

- [ ] **Step 1: Write it**

Cover, with real commands: prerequisites; the Postgres LAN opening and how to revert it; cert
minting and the SAN requirement; rocky's env file and unit; the mTLS enforcement check (both curl
cases); how to tell which host a session is on; the deploy's SSH restart; and **rollback** — re-enable
dobby's daemon, restore `CCSWITCH_SESSIOND_SOCKET`, restart the edge.

Document these failure modes explicitly:
- Cert SAN mismatch → "certificate does not match the server name".
- Protocol-version mismatch between hosts → the edge refuses the daemon; fix by syncing commits.
- sshfs mount drop → sessions on that account fail to read credentials; remount and restart the daemon.
- Both daemons enabled at once → two orchestrators over one database, the 2026-06-23 "Ready while
  working" split-brain. dobby's daemon MUST be `disable`d, not merely stopped.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/rocky-session-host.md
git commit -m "docs: add rocky session-host runbook"
```

---

## Verification

- [ ] `curl` without a client cert against `https://rocky.lan:9443/info` **fails**; with the cert it succeeds.
- [ ] A live session's `claude` process is on rocky and **not** on dobby.
- [ ] dobby's `/api/health` returns 200 and the dashboard streams that session's output.
- [ ] `psql` from rocky reaches `ccswitch` but **not** other databases.
- [ ] dobby's daemon is `disabled`, not merely stopped.
- [ ] Rollback rehearsed and returned to rocky.
- [ ] `pytest server/tests/ -q`, `mypy`, `ruff` all clean.
- [ ] No `.pem`/`.key` file is committed; the CA key lives only in `/mnt/hdd-storage/ccswitch-ca/`.
