# Onboarding a ccswitch Session Runner

**Standard work for adding a new session-runner host to the ccswitch fleet.**

Specs:
- Sub-project design (source of truth for these steps): [`docs/superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md`](../superpowers/specs/2026-08-29-multi-host-runner-dispatch-design.md) — § "Runner onboarding — the standard work"
- Umbrella design (cross-cutting decisions 1-7): [`docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md`](../superpowers/specs/2026-08-20-multi-host-session-fleet-design.md)

Placeholders: `<host>` = the new runner's short hostname (e.g. `rocky`), `<cap>` = the `max_sessions` cap for this host.

---

> **WARNING — CA reuse is mandatory for additional hosts.**
>
> `scripts/ccswitch-mint-mtls.sh` generates a **brand-new CA** on every invocation. If you run it
> for a second (or third) host it will create a fresh CA that the edge does not trust, and every
> existing host's server cert and the edge's client cert will stop working immediately.
>
> **For an additional host you must mint only a new server cert signed by the existing fleet CA.**
> See Step 3 for the exact commands. Only run `ccswitch-mint-mtls.sh` in full for the very first
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

- [ ] ccswitch repo checked out on `<host>` at a known path; venv built.
- [ ] `claude`, `node`, `git`, `python3` are on `PATH`.
- [ ] Xvfb is installed (`which Xvfb`).
- [ ] `deploy/ccswitch-daemon-remote.service` is copied / symlinked to `/etc/systemd/system/ccswitch-daemon.service`.

  ```bash
  sudo cp deploy/ccswitch-daemon-remote.service /etc/systemd/system/ccswitch-daemon.service
  sudo systemctl daemon-reload
  ```

---

### Step 3 — mTLS: mint a server cert for the new host

> **Read the CA reuse warning at the top before running any command here.**

The fleet has one CA (`/etc/ccswitch/tls/ca.pem` + `ca.key` on dobby or your secure offline store).
You need a server cert with:
- `CN=sessiond`
- `subjectAltName=DNS:<host>.tail0dd570.ts.net,IP:<tailnet-ip>`

Use the **tailnet** DNS name and tailnet IP — NOT `<host>.lan` or a LAN IP. A hostname mismatch is
the top documented footgun and will produce a TLS handshake failure.

#### Option A — mint with openssl directly (preferred for additional hosts)

```bash
# On a machine that has ca.pem + ca.key (dobby or offline key store)
OUTDIR=/tmp/mtls-<host>
mkdir -p "$OUTDIR"

# 1. Generate a server key
openssl genrsa -out "$OUTDIR/server.key" 4096

# 2. Create a CSR
openssl req -new \
  -key "$OUTDIR/server.key" \
  -subj "/CN=sessiond" \
  -out "$OUTDIR/server.csr"

# 3. Sign with the EXISTING CA (do NOT regenerate the CA)
openssl x509 -req \
  -in "$OUTDIR/server.csr" \
  -CA /etc/ccswitch/tls/ca.pem \
  -CAkey /etc/ccswitch/tls/ca.key \
  -CAcreateserial \
  -days 825 \
  -addext "subjectAltName=DNS:<host>.tail0dd570.ts.net,IP:<tailnet-ip>" \
  -out "$OUTDIR/server.pem"

# Verify
openssl verify -CAfile /etc/ccswitch/tls/ca.pem "$OUTDIR/server.pem"
openssl x509 -in "$OUTDIR/server.pem" -noout -text | grep -A2 "Subject Alternative"
```

#### Option B — use ccswitch-mint-mtls.sh (FIRST host only / fresh fleet only)

```bash
# ONLY for the very first host in a brand-new fleet.
# This generates a new CA — do NOT run for an additional host.
bash scripts/ccswitch-mint-mtls.sh /tmp/mtls <host>.tail0dd570.ts.net <tailnet-ip>
# Emits: ca.pem, server.pem, server.key (CN=sessiond), client.pem, client.key (CN=edge)
```

#### Install certs on the new host

```bash
# On <host>
sudo mkdir -p /etc/ccswitch/tls

# Copy from where you minted them:
sudo install -m 640 server.pem /etc/ccswitch/tls/server.pem
sudo install -m 600 server.key /etc/ccswitch/tls/server.key
# Copy the SHARED fleet CA (not the one just minted, if using Option A):
sudo install -m 644 /etc/ccswitch/tls/ca.pem /etc/ccswitch/tls/ca.pem   # already present, or copy from dobby
```

- [ ] `server.pem` and `server.key` installed at `/etc/ccswitch/tls/` on `<host>`.
- [ ] `ca.pem` (the shared fleet CA) installed at `/etc/ccswitch/tls/ca.pem` on `<host>`.
- [ ] Verify: `openssl verify -CAfile /etc/ccswitch/tls/ca.pem /etc/ccswitch/tls/server.pem` exits 0.

---

### Step 4 — Daemon environment file

Create `/etc/ccswitch/daemon.env` on `<host>`. This is a **dedicated** file — do NOT point the
daemon at the edge's `ccswitch.env` (see the dobby collision note in the dobby-specific section
below for why this matters).

```bash
sudo tee /etc/ccswitch/daemon.env > /dev/null <<'EOF'
# ccswitch session daemon — <host>
# DO NOT share this file with the edge process (ccswitch.env).

# Shared Postgres on dobby (tailnet address — cleartext wire; WireGuard encrypts)
CCSWITCH_DB_URL=postgresql+asyncpg://ccswitch:<password>@100.102.11.121:5432/ccswitch

# Must match the CCSWITCH_MASTER_KEY on the edge and all other daemons
CCSWITCH_MASTER_KEY=<master-key>

# This daemon's own bind endpoint (tailnet DNS + port 9443)
CCSWITCH_SESSIOND_ENDPOINT=https://<host>.tail0dd570.ts.net:9443

# mTLS — server identity
CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem

# Only the edge (CN=edge) may connect
CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge

# Xvfb for browser-based reauth
CCSWITCH_ENABLE_XVFB=true
EOF

sudo chmod 640 /etc/ccswitch/daemon.env
```

- [ ] All placeholder values (`<password>`, `<master-key>`, `<host>`) replaced with real values.
- [ ] `CCSWITCH_MASTER_KEY` matches the value on the edge and all other daemons exactly.
- [ ] File permissions: `640` (root-readable, ccswitch-group readable, not world-readable).

---

### Step 5 — Account store mount

The canonical account store lives on dobby (`~/.claude-accounts/cfg/` and `~/.claude-accounts/.claude`).
Workers mount it via a systemd mount unit modelled on `ccswitch-mounts.service` (rocky's reference).

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
sudo systemctl enable --now ccswitch-daemon.service
sudo systemctl status ccswitch-daemon.service
```

- [ ] Service is `active (running)`.
- [ ] No TLS errors in `journalctl -u ccswitch-daemon.service -n 50`.
- [ ] Daemon bound on port 9443: `ss -tlnp | grep 9443`.

---

### Step 7 — Register with the fleet

Add the new host to the fleet registry. Choose one method:

**Fleet UI (preferred):**
1. Open the Fleet UI in the ccswitch web interface.
2. `POST /hosts` (or use the UI add-host dialog) with:
   - `name`: `<host>`
   - `endpoint`: `https://<host>.tail0dd570.ts.net:9443`
   - `enabled`: `true`
   - `max_sessions`: `<cap>`

**Environment seed (alternative):**
```bash
# Add to the edge's ccswitch.env alongside the existing CCSWITCH_FLEET_HOSTS entry,
# then restart the edge. Format is a comma-separated list of name=endpoint pairs.
CCSWITCH_FLEET_HOSTS=rocky=https://rocky.tail0dd570.ts.net:9443,<host>=https://<host>.tail0dd570.ts.net:9443
```

- [ ] Host appears in `GET /api/fleet/hosts` response.
- [ ] `max_sessions` is set to `<cap>` (protects the host from session starvation; the dispatcher respects it automatically).

---

### Step 8 — Verify

```bash
# Check fleet health via the API (from the edge host or any machine with access)
curl -s https://dobby.tail0dd570.ts.net:8787/api/fleet/hosts | jq '.[] | select(.name=="<host>")'
```

Expected: `"healthy": true`, `sha` matches the edge's deployed SHA, `protocol` matches.

> A `healthy: false` with `last_error: "protocol mismatch"` means the runner is running a different
> ccswitch version than the edge. Restart the daemon after updating the checkout. Sessions will not
> be dispatched to an unhealthy host — it is excluded from ranking without affecting the edge or
> other hosts.

- [ ] `healthy: true`.
- [ ] `sha` matches the edge.
- [ ] `protocol` matches.
- [ ] Smoke test: create a session with the Runner override set to `<host>`. Confirm:
  - The `HostBadge` in the session header shows `<host>`.
  - The PTY is running on `<host>` (check `journalctl -u ccswitch-daemon.service -f` on `<host>` while attaching).

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
- [x] `CCSWITCH_MASTER_KEY` is present in the edge's `ccswitch.env`.
- [x] Account store is canonical on dobby — **no mount needed** (dobby is the source).
- [ ] Verify Xvfb is available: `which Xvfb` — it should be present; ensure `CCSWITCH_ENABLE_XVFB=true` is in `daemon.env`.

### What is missing on dobby

**Server cert** — dobby currently holds only a CA cert and a client cert (CN=edge). It has no server
cert to bind on port 9443. Mint one using Option A above:

```bash
# On dobby (has ca.pem + ca.key locally)
OUTDIR=/tmp/mtls-dobby
mkdir -p "$OUTDIR"

openssl genrsa -out "$OUTDIR/server.key" 4096

openssl req -new \
  -key "$OUTDIR/server.key" \
  -subj "/CN=sessiond" \
  -out "$OUTDIR/server.csr"

openssl x509 -req \
  -in "$OUTDIR/server.csr" \
  -CA /etc/ccswitch/tls/ca.pem \
  -CAkey /etc/ccswitch/tls/ca.key \
  -CAcreateserial \
  -days 825 \
  -addext "subjectAltName=DNS:dobby.tail0dd570.ts.net,IP:100.102.11.121" \
  -out "$OUTDIR/server.pem"

openssl verify -CAfile /etc/ccswitch/tls/ca.pem "$OUTDIR/server.pem"

sudo install -m 640 "$OUTDIR/server.pem" /etc/ccswitch/tls/server.pem
sudo install -m 600 "$OUTDIR/server.key" /etc/ccswitch/tls/server.key
```

### Env collision — dedicated `daemon.env` is mandatory on dobby

dobby's `ccswitch-daemon.service` must NOT inherit the edge's `/etc/ccswitch/ccswitch.env`. That
file contains `CCSWITCH_SESSIOND_ENDPOINT=https://rocky:9443` — the edge's *target* daemon. If the
dobby daemon reads that as its own *bind* address, it will try to bind `rocky:9443` instead of its
own tailnet address, and the service will fail or silently misconfigure.

**Resolution:** create `/etc/ccswitch/daemon.env` (Step 4 above with dobby's values) and point the
daemon unit at it via a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/ccswitch-daemon.service.d
sudo tee /etc/systemd/system/ccswitch-daemon.service.d/env-file.conf > /dev/null <<'EOF'
[Service]
# Replace the default EnvironmentFile with the daemon-specific one
# so it does not inherit the edge's CCSWITCH_SESSIOND_ENDPOINT.
EnvironmentFile=
EnvironmentFile=/etc/ccswitch/daemon.env
EOF

sudo systemctl daemon-reload
```

Then create `/etc/ccswitch/daemon.env` for dobby:

```
CCSWITCH_DB_URL=postgresql+asyncpg://ccswitch:<password>@100.102.11.121:5432/ccswitch
CCSWITCH_MASTER_KEY=<master-key>
CCSWITCH_SESSIOND_ENDPOINT=https://dobby.tail0dd570.ts.net:9443
CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem
CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge
CCSWITCH_ENABLE_XVFB=true
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
