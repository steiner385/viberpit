# Onboarding a ViberPit Session Runner

**Standard work for adding a new session-runner host to the ViberPit fleet.**

Specs:
- Sub-project design (source of truth for these steps): [`docs/superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md`](../superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md) — § "Runner onboarding — the standard work"
- Umbrella design (cross-cutting decisions 1-7): [`docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md`](../superpowers/specs/2026-08-20-multi-host-session-fleet-design.md)

Placeholders: `<host>` = the new runner's short hostname (e.g. `rocky`), `<cap>` = the `max_sessions` cap for this host.

---

> **WARNING — CA reuse is mandatory for additional hosts.**
>
> `scripts/viberpit-mint-mtls.sh` generates a **brand-new CA** on every invocation. A fresh CA is
> one the edge does not trust, so minting one for a second (or third) host would break every
> existing host's server cert and the edge's client cert immediately. The script now guards this
> itself: it refuses to run when a CA already exists in `/etc/viberpit/tls/` (override dir via
> `VIBERPIT_TLS_DIR`), and it installs `ca.pem` + `ca.key` (0600) there durably on a first mint
> instead of leaving the key behind in the outdir (issue #841).
>
> **For an additional host you must mint only a new server cert signed by the existing fleet CA.**
> See Step 3 for the exact commands. Only run `viberpit-mint-mtls.sh` in full for the very first
> host ever added to a fresh fleet.

---

## Checklist

### Step 1 — Network

- [ ] `tailscaled` is running on `<host>` and the host appears in `tailscale status`.
- [ ] Confirm the host's tailnet IP and tailnet DNS name (e.g. `<host>.tail0dd570.ts.net`).
- [ ] The edge (dobby) can reach `https://<host>.tail0dd570.ts.net:9443` — check firewall / `ufw allow 9443`.
- [ ] **Postgres (on dobby) must permit the new host.** Postgres binds the tailnet address only — no LAN listener (umbrella decision 7; the PG wire is cleartext; WireGuard on the tailnet is the encryption layer).

  On dobby, as the postgres superuser:

  ```sql
  -- allow the new host's tailnet IP to connect as the ccswitch role to the ccswitch database
  -- (add this to pg_hba.conf, then reload)
  host    ccswitch        ccswitch        <tailnet-ip>/32         scram-sha-256
  ```

  ```bash
  sudo systemctl reload postgresql
  # verify:
  psql -h <tailnet-ip> -U ccswitch -d ccswitch -c "SELECT 1;"
  ```

  > Umbrella decision 7 rationale: `pg_hba` `/32` limits *who can connect*, not *who can read*. The
  > tailnet WireGuard layer encrypts the cleartext Postgres wire. No LAN listener is created.

---

### Step 2 — Runtime

- [ ] ViberPit repo checked out on `<host>` at a known path; venv built.
- [ ] `claude`, `node`, `git`, `python3` are on `PATH`.
- [ ] Xvfb is installed (`which Xvfb`).
- [ ] `deploy/viberpit-daemon-remote.service` is copied / symlinked to `/etc/systemd/system/viberpit-daemon.service`.

  ```bash
  sudo cp deploy/viberpit-daemon-remote.service /etc/systemd/system/viberpit-daemon.service
  sudo systemctl daemon-reload
  ```

---

### Step 3 — mTLS: mint a server cert for the new host

> **Read the CA reuse warning at the top before running any command here.**

The fleet runs on **one CA, `viberpit-fleet-ca-3`**, since 2026-10-07. Both earlier CAs (the original
fleet CA and ca2, issue #841) lacked a `keyUsage` extension. Python 3.13+ verifies
with `VERIFY_X509_STRICT` and rejects such a CA, so the Ubuntu 26.04 upgrade (Python 3.14) cut the edge
off from every daemon, and the fleet was re-keyed onto ca3:

| Material | Location | Role |
|---|---|---|
| `ca3.pem` + `ca3.key` | `/etc/viberpit/tls/` on dobby (key 0600, root); backup in 1Password (Private vault, "ViberPit fleet CA 3") | **The signing CA for all new certs, server and client.** |
| `ca.pem` | `/etc/viberpit/tls/` on dobby and every runner | A copy of `ca3.pem`. Daemons verify the edge's client cert against it (`VIBERPIT_SESSIOND_TLS_CA`). |
| `ca-bundle.pem` | `/etc/viberpit/tls/` on dobby | Also just `ca3.pem` now. The edge verifies daemon server certs against it (`VIBERPIT_SESSIOND_CLIENT_CA`). |
| `ca.key`, `ca2.*` (retired CAs) | `/mnt/hdd-storage/ccswitch-ca/ca.key` (**root-only directory**) and `/etc/viberpit/tls/ca2.*` on dobby | Retired; nothing chains to them. Do not sign with them. |

Any CA you mint MUST carry `keyUsage=critical,keyCertSign,cRLSign`. Check with
`openssl x509 -in <ca>.pem -noout -ext keyUsage` before trusting it anywhere.

> The root-only directory matters: searching for `ca.key` as a non-root user reports permission
> denied / nothing, which reads exactly like the key being gone — that is how it was mis-declared
> lost on 2026-08-30. `sudo ls /mnt/hdd-storage/ccswitch-ca/` before concluding a key is lost.

You need a server cert with:
- `CN=sessiond`
- `subjectAltName=DNS:<host>.tail0dd570.ts.net,IP:<tailnet-ip>`

Use the **tailnet** DNS name and tailnet IP — NOT `<host>.lan` or a LAN IP. A hostname mismatch is
the top documented footgun and will produce a TLS handshake failure.

#### Option A — mint with openssl directly (preferred for additional hosts)

```bash
# On dobby (has ca3.pem + ca3.key; run the signing step with sudo — ca3.key is root-only)
OUTDIR=/tmp/mtls-<host>
mkdir -p "$OUTDIR"

# 1. Generate a server key
openssl genrsa -out "$OUTDIR/server.key" 4096

# 2. Create a CSR
openssl req -new \
  -key "$OUTDIR/server.key" \
  -subj "/CN=sessiond" \
  -out "$OUTDIR/server.csr"

# 3. Sign with the EXISTING signing CA — ca3, NOT a fresh one and NOT a retired one.
#    `openssl x509` takes extensions only via -extfile (it has no -addext option).
printf 'subjectAltName=DNS:<host>.tail0dd570.ts.net,IP:<tailnet-ip>\nextendedKeyUsage=serverAuth\n' \
  > "$OUTDIR/server.ext"
sudo openssl x509 -req \
  -in "$OUTDIR/server.csr" \
  -CA /etc/viberpit/tls/ca3.pem \
  -CAkey /etc/viberpit/tls/ca3.key \
  -CAserial /etc/viberpit/tls/ca3.srl \
  -days 825 \
  -extfile "$OUTDIR/server.ext" \
  -out "$OUTDIR/server.pem"

# Verify against what the edge actually trusts, with the same strict rules Python 3.13+ applies
openssl verify -x509_strict -CAfile /etc/viberpit/tls/ca-bundle.pem "$OUTDIR/server.pem"
openssl x509 -in "$OUTDIR/server.pem" -noout -text | grep -A2 "Subject Alternative"
```

#### Option B — use viberpit-mint-mtls.sh (FIRST host only / fresh fleet only)

```bash
# ONLY for the very first host in a brand-new fleet.
# Generates a new CA — refuses to run if /etc/viberpit/tls/ already holds one, and
# installs ca.pem + ca.key (0600) there durably (needs sudo for that reason).
sudo bash scripts/viberpit-mint-mtls.sh /tmp/mtls <host>.tail0dd570.ts.net <tailnet-ip>
# Emits: ca.pem, server.pem, server.key (CN=sessiond), client.pem, client.key (CN=edge)
```

#### Install certs on the new host

```bash
# On <host>
sudo mkdir -p /etc/viberpit/tls

# Copy from where you minted them:
sudo install -m 640 server.pem /etc/viberpit/tls/server.pem
sudo install -m 600 server.key /etc/viberpit/tls/server.key
# Copy the fleet CA cert (dobby's ca.pem is ca3): the daemon uses it to verify the edge's
# client cert, which ca3 signed.
scp dobby:/etc/viberpit/tls/ca3.pem /tmp/ca.pem && sudo install -m 644 /tmp/ca.pem /etc/viberpit/tls/ca.pem
```

- [ ] `server.pem` and `server.key` installed at `/etc/viberpit/tls/` on `<host>`.
- [ ] `ca.pem` (a copy of `ca3.pem`) installed at `/etc/viberpit/tls/ca.pem` on `<host>`.
- [ ] Verify the server cert the way Python 3.13+ will: `openssl verify -x509_strict -CAfile /etc/viberpit/tls/ca.pem /etc/viberpit/tls/server.pem` exits 0.

#### Lost or rotated CA — the bundle pattern

If the signing CA key is ever genuinely unavailable, do NOT re-key existing hosts. Bridge in a new
CA instead (this is exactly how ca2 was introduced on 2026-08-30). The bridge only holds while EVERY
CA in the bundle carries `keyUsage`. A strict verifier rejects a chain through a CA without it, which
is why 2026-10-07 needed a full re-key onto ca3 rather than a bridge.

1. Mint a new durable CA on dobby: `caN.pem` + `caN.key` (0600) in `/etc/viberpit/tls/`.
2. Sign all NEW hosts' server certs with `caN`. Existing hosts keep their certs untouched.
3. Concatenate what the edge trusts: `cat ca.pem caN.pem | sudo tee /etc/viberpit/tls/ca-bundle.pem`
   and point `VIBERPIT_SESSIOND_CLIENT_CA` at the bundle in the edge's `viberpit.env`, then restart
   the edge.
4. Leave each daemon's `VIBERPIT_SESSIOND_TLS_CA` pointing at the CA that signed the edge's
   *client* cert (ca3 today) — daemons verify the edge, not each other, so they
   only need the client cert's CA.

---

### Step 4 — Daemon environment file

Create `/etc/viberpit/daemon.env` on `<host>`. This is a **dedicated** file — do NOT point the
daemon at the edge's `viberpit.env` (see the dobby collision note in the dobby-specific section
below for why this matters).

```bash
sudo tee /etc/viberpit/daemon.env > /dev/null <<'EOF'
# ViberPit session daemon — <host>
# DO NOT share this file with the edge process (viberpit.env).

# Shared Postgres on dobby (tailnet address — cleartext wire; WireGuard encrypts)
VIBERPIT_DB_URL=postgresql+asyncpg://ccswitch:<password>@100.102.11.121:5432/ccswitch

# Must match the VIBERPIT_MASTER_KEY on the edge and all other daemons. This is ALSO
# the key the daemon signs cross-process bearer tokens with (the assistant drawer's
# ccas1 token, the internal ccsys1 token) and the edge verifies them with — the
# edge's session secret is cookie-only and must NOT be needed here.
VIBERPIT_MASTER_KEY=<master-key>

# This daemon's own bind endpoint (tailnet DNS + port 9443)
VIBERPIT_SESSIOND_ENDPOINT=https://<host>.tail0dd570.ts.net:9443

# mTLS — server identity
VIBERPIT_SESSIOND_TLS_CERT=/etc/viberpit/tls/server.pem
VIBERPIT_SESSIOND_TLS_KEY=/etc/viberpit/tls/server.key
# TLS_CA verifies the EDGE'S CLIENT CERT — the fleet CA (ca3) that signed client.pem.
# See Step 3's CA table.
VIBERPIT_SESSIOND_TLS_CA=/etc/viberpit/tls/ca.pem

# Only the edge (CN=edge) may connect
VIBERPIT_SESSIOND_CLIENT_ALLOWLIST=edge

# Fleet identity (#852): the hosts.name the dispatcher stamps onto sessions sent
# here. The default (kernel hostname's first label) matches auto-registered
# tailnet hosts; REQUIRED explicitly whenever the registry row uses another
# name — a mismatched identity rehydrates ZERO sessions after a restart.
VIBERPIT_HOST_NAME=<host>

# Daemon role: every runner beyond the fleet's ONE primary must be a worker.
# Any other value fails the daemon boot loudly (by design).
VIBERPIT_DAEMON_ROLE=worker

# Xvfb for browser-based reauth
VIBERPIT_ENABLE_XVFB=true
EOF

sudo chmod 640 /etc/viberpit/daemon.env
```

- [ ] All placeholder values (`<password>`, `<master-key>`, `<host>`) replaced with real values.
- [ ] `VIBERPIT_MASTER_KEY` matches the value on the edge and all other daemons exactly.
- [ ] `VIBERPIT_HOST_NAME` matches this host's row in the fleet registry (Fleet UI / `hosts` table)
  exactly — it partitions session ownership (#852): a worker rehydrates only rows stamped with
  its own name, and every daemon's recovery loops (stuck-session sweep, rotation monitor,
  disabled-account sweep) act only on sessions it owns. Rows with `host = NULL` (pre-fleet
  legacy + the primary's own automation spawns) belong to the primary, which also adopts rows
  whose host has been removed/disabled from the registry.
- [ ] `VIBERPIT_DAEMON_ROLE=worker` is set — exactly one daemon in the fleet (the primary) omits
  it or sets `primary`. The primary additionally holds the DB-side **primary lease** (#852,
  `daemon_leases` row): a second daemon booting as primary fails `start()` loudly while the
  real primary's heartbeat is fresh (renewed every `VIBERPIT_PRIMARY_LEASE_TTL_S`/5, default
  TTL 300 s). A clean shutdown releases the lease immediately; after a primary crash, a
  replacement primary on another host waits out at most one TTL. A worker's usage poller runs
  READ-ONLY (`allow_refresh=False`): it feeds proactive ceiling rotation and dead-account
  detection for its own sessions without racing the primary over the shared refresh-token
  files.
- [ ] File permissions: `640` (root-readable, ViberPit-service-group readable, not world-readable).

---

### Step 5 — Account store mount

The canonical account store lives on dobby (`~/.claude-accounts/cfg/` and `~/.claude-accounts/.claude`).
Workers mount it via a systemd mount unit modelled on `viberpit-mounts.service` (rocky's reference).

> Umbrella decisions 4-5: credentials are shared from dobby, never copied. A single `.credentials.json`
> per account prevents refresh-token divergence. The directory (not a symlink) is mounted so that
> write-temp-then-rename saves do not silently break the link.

- [ ] Mount each account's `cfg/<account>/` from dobby on `<host>`.
- [ ] Mount the shared `.claude/` from dobby on `<host>`.
- [ ] Mount unit has retry configured (or autofs); define daemon behavior if mount vanishes mid-turn.
- [ ] Verify mounts are live: `ls ~/.claude-accounts/cfg/` on `<host>` lists the same accounts as on dobby.

> **If `<host>` IS dobby:** no mount needed — dobby is the source. Skip this step.

---

### Step 6 — Start the daemon

```bash
sudo systemctl enable --now viberpit-daemon.service
sudo systemctl status viberpit-daemon.service
```

- [ ] Service is `active (running)`.
- [ ] No TLS errors in `journalctl -u viberpit-daemon.service -n 50`.
- [ ] Daemon bound on port 9443: `ss -tlnp | grep 9443`.

---

### Step 7 — Register with the fleet

Add the new host to the fleet registry. Choose one method:

**Fleet UI (preferred):**
1. Open the Fleet UI in the ViberPit web interface.
2. `POST /hosts` (or use the UI add-host dialog) with:
   - `name`: `<host>`
   - `endpoint`: `https://<host>.tail0dd570.ts.net:9443`
   - `enabled`: `true`
   - `max_sessions`: `<cap>`

> **Updates are `PUT`, not `PATCH`.** Host mutation is `PUT /api/fleet/hosts/{name}` — partial
> bodies are fine (omitted/`null` fields are left alone, not nulled). `PATCH` is not routed and
> returns a 405 whose body is easy to mis-parse as success when scripting — check the status code.
> `maxSessions` can be set on the create `POST`; on-the-fly changes go through `PUT`.

**Environment seed (alternative):**
```bash
# Add to the edge's viberpit.env alongside the existing VIBERPIT_FLEET_HOSTS entry,
# then restart the edge. Format is a comma-separated list of name=endpoint pairs.
VIBERPIT_FLEET_HOSTS=rocky=https://rocky.tail0dd570.ts.net:9443,<host>=https://<host>.tail0dd570.ts.net:9443
```

> **NOTE — edge restart required for newly added hosts (current behaviour).**
> The fleet router builds its per-host clients at edge startup. Adding or enabling a new host via
> the Fleet UI takes effect only after the edge process is restarted (`sudo systemctl restart
> viberpit.service` on dobby). Disabling or removing a host IS live and requires no restart.
> (Lazy per-pick reconciliation — picking up new hosts without a restart — is a deferred
> follow-up.)

- [ ] Host appears in `GET /api/fleet/hosts` response.
- [ ] `max_sessions` is set to `<cap>` (protects the host from session starvation; the dispatcher respects it automatically).
- [ ] Set `sshHost` (`[user@]host`) and `repoPath` via `PUT /api/fleet/hosts/{name}` or the Fleet UI
  — a deploy refuses (`unconfigured`) an online runner without them.

---

### Step 8 — Verify

```bash
# Check fleet health via the API (from the edge host or any machine with access)
curl -s https://dobby.tail0dd570.ts.net/api/fleet/hosts | jq '.[] | select(.name=="<host>")'
```

Expected: `"healthy": true`, `sha` matches the edge's deployed SHA, `protocol` matches.

> A `healthy: false` with `last_error: "protocol mismatch"` means the runner is running a different
> ViberPit version than the edge. Restart the daemon after updating the checkout. Sessions will not
> be dispatched to an unhealthy host — it is excluded from ranking without affecting the edge or
> other hosts.

- [ ] `healthy: true`.
- [ ] `sha` matches the edge.
- [ ] `protocol` matches.
- [ ] `checkoutSha` and `sha` both match the edge; `lastError` empty (a `version skew: …` there
  means the runner is fenced until updated).
- [ ] Smoke test: create a session with the Runner override set to `<host>`. Confirm:
  - The `HostBadge` in the session header shows `<host>`.
  - The PTY is running on `<host>` (check `journalctl -u viberpit-daemon.service -f` on `<host>` while attaching).

---

### Step 9 — Codex (optional)

Skip this unless the host should also run Codex sessions. A runner with no Codex
stays `healthy`, keeps taking Claude sessions, and remains a deploy target — the
daemon reports `binary_not_found` through `/info.engines` and drops out of *Codex*
dispatch only. Codex is never a boot dependency.

The full procedure — pinned binary install, the authority's separate PKI, the
client CN this host must present, and the six-check preflight — is
`docs/runbooks/codex-credential-authority.md` § 3. In outline:

```bash
bash scripts/fetch-codex.sh                       # pinned + digest-verified
# install codex-runner:<host>.pem/.key (0600) + server-ca.pem, add the CN to the
# authority's VIBERPIT_CODEX_AUTHORITY_CLIENT_ALLOWLIST, restart the authority
python -m server.codex.doctor                     # exits 0 when nothing failed
```

- [ ] `python -m server.codex.doctor` reports no FAIL.
- [ ] `find /var/lib/viberpit/state/ccswitch/codex -name auth.json` prints nothing
      — a runner must never hold a refresh token.
- [ ] Both launch flags stay `false` until the controlled-enablement bar has passed.

> The unit sets `Environment=XDG_STATE_HOME=%S/viberpit/state` so per-account
> `CODEX_HOME`s land on systemd's host-local `StateDirectory`. If you install the
> unit by hand, `daemon-reload` — otherwise Codex homes follow
> `$HOME/.local/state`, which on a runner with sshfs-mounted state is refused
> (correctly) as not provably local.

---

### Step 10 — Prove the runner can finish repository work

A healthy daemon and a passing Codex credential doctor do not establish that an agent can
run a project's tests, commit, or publish a PR. Accept the runner only after checking both
**host capabilities** and the **effective session permissions**. Repeat this check after an
engine upgrade or a permission-policy change.

#### 10a. Check the host as the daemon's user

On the runner, inspect the service identity without printing its environment or credentials:

```bash
systemctl show viberpit-daemon.service -p User -p WorkingDirectory -p MainPID
hostname
id -un
command -v git gh python3 node pnpm ssh
```

Run the remaining probes as that user, in an actual disposable project worktree. A terminal
logged in as a different user is not evidence that the daemon has the same PATH, GitHub
credentials, or filesystem ownership. Compare the tools visible inside a ViberPit session too.
For Python-only projects, Node/pnpm are not prerequisites; derive tool versions from the target
repository's manifests and CI instead of installing a runner-wide guessed version.

```bash
# Replace this with the target worktree path; do not run against the runner's daemon checkout.
cd /path/to/project-worktree

git --version
node --version
pnpm --version

# Network + GitHub authentication; prints a login, never a token.
gh api graphql -f query='{viewer{login}}'

# Local test-server socket and a disposable write beside the REAL worktree Git metadata.
# Does not alter the index, refs, Git configuration, or tracked files.
python3 - <<'PY'
import socket
import subprocess
import tempfile

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    print("loopback listen: PASS")

git_dir = subprocess.check_output(
    ["git", "rev-parse", "--absolute-git-dir"], text=True,
).strip()
with tempfile.TemporaryFile(dir=git_dir):
    print("worktree Git metadata write: PASS")
PY
```

- [ ] Required binaries resolve and match the project's supported versions.
- [ ] GitHub authentication works for the service user; verify access to the target repository
      as well. A successful `viewer` query alone does not prove push permission.
- [ ] A loopback listener opens (HTTP integration tests and local browser tests need it).
- [ ] The worktree's actual Git metadata is writable. Linked worktrees store this outside the
      visible `.git` file, so checking only the project directory is insufficient.
- [ ] Install locked project dependencies and run its documented build/setup sequence and
      mandatory commit gates. Browser projects additionally need the pinned browser and OS
      libraries, a fresh SPA build, and an isolated test database where required. Do not reset
      a shared database as an onboarding probe or reuse a person's signed-in browser profile.

If a command is missing, install it using the project's supported setup procedure. If GitHub
reports authentication failure, configure the service user's authorized GitHub credential flow;
never copy Codex refresh credentials to a worker (Step 9). A failed host socket/metadata probe
needs investigation of ownership, mount options, or service confinement. Do not use recursive
`chmod`, disable host security, or bypass Git hooks to make onboarding appear green.

#### 10b. Check the effective Codex session, not just its mode label

The canonical mapping is [`server/codex/permissions.py`](../../server/codex/permissions.py).
The distinction between sandbox boundaries and approval policy is also documented in
[OpenAI's sandbox guide](https://learn.chatgpt.com/docs/sandboxing).

| ViberPit mode | Codex sandbox | Approval policy | Expected behavior |
|---|---|---|---|
| `full-auto` (after #1452) | `workspace-write` | `on-request` | Routine workspace work proceeds; commands needing network, protected Git writes, or listening sockets request approval. |
| Old, broken `full-auto` mapping | `workspace-write` | `never` | Restricted commands fail with no approval route; not acceptable for repository-delivery work. |
| `bypassPermissions` | `danger-full-access` | `never` | No sandbox or approval barrier. Explicit operator opt-in only, where the host is the intended boundary. |

These are ViberPit's pinned-engine profiles, not universal defaults for every Codex version.
Setting a mode label to `full-auto` does not itself grant network access. Do not edit a personal
`~/.codex/config.toml` to repair a ViberPit-managed thread: the runner uses per-account runtime
homes and the driver supplies the session policy. Unattended automation additionally passes
`unattended_launch_blocker`; do not assume an interactive approval workflow makes an automation
eligible or silently switch it to `bypassPermissions`.

1. Create a Codex **coding session** with the Runner override set to the new host. The assistant
   drawer intentionally has a different, pinned read-only policy and is not this smoke test.
2. Check the effective instructions/policy: `workspace-write` must have an approval channel.
   Run the same socket and GitHub probes through the agent's shell tool. Sandbox denial is
   expected for confined commands; ask for the approved **local** retry and verify it works.
3. Confirm a real approval card reaches the operator, that declining prevents the operation,
   and that approving a fresh retry allows the same operation on the same runner.
4. In the project's disposable feature branch, finish the normal test/commit/push/draft-PR
   workflow, with operator authorization for publication and without bypassing any gates.
   Close a probe-only PR rather than merging a meaningless test commit. An authorized real
   task's successfully published PR can satisfy this check instead.

For a session created under the old mapping, first ensure the runner is on a release containing
[#1452](https://github.com/steiner385/viberpit/pull/1452) through the normal deploy controller.
Reapply `full-auto` in that session's permission control and verify the effective policy on the
next turn. The equivalent authenticated API request from an allowed tailnet client is:

```bash
VIBERPIT_URL=https://dobby.tail0dd570.ts.net
SESSION_ID=replace-with-the-coding-session-uuid
curl --connect-timeout 5 --max-time 20 --fail-with-body --silent --show-error \
  -X POST "$VIBERPIT_URL/api/sessions/$SESSION_ID/permission-mode" \
  -H 'Content-Type: application/json' \
  --data '{"mode":"full-auto"}'
```

Use the configured HTTPS edge origin (the default above is documented in
[`deploy/README.md`](../../deploy/README.md#https-via-nginx--tailscale-cert-required-once-per-host));
port 8787 is loopback-only. A TLS error needs the edge certificate-renewal procedure in that
runbook, not `curl -k`. Alternatively, use the session permission control through an already
working, authenticated ViberPit UI.

A successful HTTP response is not the acceptance test: confirm an actual local approval/retry.
If a resumed thread still advertises the old policy, start a fresh coding session after the
runner update, preserving its branch and work. Do not restart the daemon while other sessions
are active just to refresh a permission setting; it owns their live processes.

#### Troubleshooting decision and Rocky evidence

| Observation | Next action |
|---|---|
| Host terminal and approved local command pass; sandboxed command fails | Fix/reapply session policy or request local escalation. No package install or remote host is indicated. |
| Host terminal passes; approved local command fails | Compare the daemon user, PATH, credentials, mounts, and service confinement. |
| Neither execution context passes | Repair the specific host prerequisite before accepting the runner. |
| Approval is unavailable or rejected | Record the exact policy/rejection and report the runner limitation. Do not route through SSH or another tool to evade it. |

On 2026-09-08, Rocky passed GitHub GraphQL authentication, a loopback listener, and a temporary
write in the KinDash linked-worktree Git directory when the local command was approved. It had
Node `v22.23.2`, pnpm `11.25.0`, `gh`, and `ssh`; the daemon and terminal both ran as `tony`.
The earlier kiosk-fix session had `workspace-write` + `never`: test sockets and SSH failed with
`EPERM`, and protected Git writes were unavailable. This was a session-policy defect, not a
missing Rocky package. The fix was published from Dobby as KinDash #15798, but after #1452 and
an effective `on-request` policy, the first recovery choice should have been a local retry.
These host versions are incident evidence, not pins to install on future runners.

- [ ] Record runner, engine version, mode, effective sandbox/approval policy, project/worktree,
      and which probes/gates passed; include a PR link when delivery was exercised.
- [ ] Retry locally after permission changes before proposing a host move. Reserve Dobby for a
      verified host-specific dependency or an explicitly authorized move, not an assumed escape
      from sandbox restrictions.

---

## Rollback

No edge restart is needed.

1. In the Fleet UI: disable or remove `<host>` from the registry.
2. The registry drops to a single host → `FleetOrchestrator` degrades to the remaining host(s) automatically.
3. Any sessions already running on `<host>` remain attached to their owning daemon until they end; new sessions are dispatched only to the remaining enabled host(s).

---

## dobby-specific application

dobby is the edge host, so most prerequisites are already satisfied. Two items are missing and one
collision must be avoided:

### What is already satisfied on dobby

- [x] Postgres is local (dobby hosts the shared PG instance — no `/32` rule needed for self).
- [x] `VIBERPIT_MASTER_KEY` is present in the edge's `viberpit.env`.
- [x] Account store is canonical on dobby — **no mount needed** (dobby is the source).
- [ ] Verify Xvfb is available: `which Xvfb` — it should be present; ensure `VIBERPIT_ENABLE_XVFB=true` is in `daemon.env`.

### What is missing on dobby

**Server cert** — *re-keyed 2026-10-07*: dobby's `/etc/viberpit/tls/server.pem` + `server.key` exist
and are **ca3-signed**. For renewal, mint with Option A above (sign with `ca3.key`, verify with
`-x509_strict` against `ca-bundle.pem`), then:

```bash
sudo install -m 640 "$OUTDIR/server.pem" /etc/viberpit/tls/server.pem
sudo install -m 600 "$OUTDIR/server.key" /etc/viberpit/tls/server.key
```

### Env collision — dedicated `daemon.env` is mandatory on dobby

dobby's `viberpit-daemon.service` must NOT inherit the edge's `/etc/viberpit/viberpit.env`. That
file contains `VIBERPIT_SESSIOND_ENDPOINT=https://rocky:9443` — the edge's *target* daemon. If the
dobby daemon reads that as its own *bind* address, it will try to bind `rocky:9443` instead of its
own tailnet address, and the service will fail or silently misconfigure.

**Resolution:** create `/etc/viberpit/daemon.env` (Step 4 above with dobby's values) and point the
daemon unit at it via a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/viberpit-daemon.service.d
sudo tee /etc/systemd/system/viberpit-daemon.service.d/env-file.conf > /dev/null <<'EOF'
[Service]
# Replace the default EnvironmentFile with the daemon-specific one
# so it does not inherit the edge's VIBERPIT_SESSIOND_ENDPOINT.
EnvironmentFile=
EnvironmentFile=/etc/viberpit/daemon.env
EOF

sudo systemctl daemon-reload
```

Then create `/etc/viberpit/daemon.env` for dobby:

```
VIBERPIT_DB_URL=postgresql+asyncpg://ccswitch:<password>@100.102.11.121:5432/ccswitch
VIBERPIT_MASTER_KEY=<master-key>
VIBERPIT_SESSIOND_ENDPOINT=https://dobby.tail0dd570.ts.net:9443
VIBERPIT_SESSIOND_TLS_CERT=/etc/viberpit/tls/server.pem
VIBERPIT_SESSIOND_TLS_KEY=/etc/viberpit/tls/server.key
VIBERPIT_SESSIOND_TLS_CA=/etc/viberpit/tls/ca.pem
VIBERPIT_SESSIOND_CLIENT_ALLOWLIST=edge
VIBERPIT_ENABLE_XVFB=true
```

### dobby registration

Register dobby exactly as Step 7 describes:

- endpoint: `https://dobby.tail0dd570.ts.net:9443`
- `enabled: true`
- `max_sessions: <cap>` (choose a cap that leaves headroom for the edge, UI, desktop, and other
  local processes — dobby has 16 cores but they are shared with 1Password, claude-desktop, Chrome,
  and session work already in flight)

Then run the Step 8 verification: `healthy: true`, SHA + protocol match, smoke-test session lands
on dobby with the HostBadge confirming it.
