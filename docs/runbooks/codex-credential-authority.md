# Codex credential authority runbook

Host procedures for `viberpit-codex-auth.service` and the Codex runners it serves:
homes and permissions, its own PKI, onboarding a runner, reauth, backup/restore,
version upgrades, rollback, and the kill switch.

`deploy/README.md` § "ChatGPT credential authority" is the architectural overview
and the install/env reference — read it first and do not duplicate it here. This
document is the *procedures*.

**One-line model.** The authority is the only process in the fleet holding an
OpenAI **refresh** token. Runners hold none: they fetch short-lived access-token
envelopes over mutual TLS and hand them straight to `codex app-server` over stdin.
Everything below exists to keep that true.

---

## 1. The two kinds of Codex home — never confuse them

They have the same *shape* (a `CODEX_HOME`, 0700, created by
`server/codex/process.py:ensure_codex_home`) and opposite *contents*.

| | Authority managed home | Runner home |
|---|---|---|
| Path | `/var/lib/ccswitch-codex-auth/accounts/<account>/` | `/var/lib/viberpit/state/ccswitch/codex/<account>/` |
| Owner | `ccswitch-auth` | `tony` (the session-daemon user) |
| Holds | `auth.json` — **refresh token** | thread SQLite + WAL, history, shell snapshots |
| Written by | `codex login` / `logout`, driven by the authority | `codex app-server`, driven by the daemon |
| Read by | `server/codex/auth_store.py` and nothing else | the app-server child only |
| May a session user read it? | **No.** That is the whole service. | Yes — it is that user's own state |

Two standing invariants, both machine-checked:

* **A runner home must never contain `auth.json`.** The manager asserts this
  immediately after every login (`external_auth.assert_no_runner_credentials`) and
  refuses to keep the runner rather than leave a refresh token on a session host.
* **Neither home may be on a network filesystem.** `assert_local_filesystem` fails
  closed on a known network fstype *and* on one it cannot prove local, and
  `ensure_codex_home` refuses a symlink anywhere in the path — which is how a
  runner home would otherwise be aimed at the authority's. Codex keeps SQLite/WAL
  there, and #1005 is what a FUSE mount does to a format that assumes local
  locking.

The daemon units pin the runner base with `Environment=XDG_STATE_HOME=%S/viberpit/state`
so locality is a property of the unit rather than a hope about where
`$HOME/.local/state` resolves on a given runner.

### Verify the isolation

```bash
# The session user must not be able to read the credential store.
sudo -u tony test -r /var/lib/ccswitch-codex-auth/accounts && echo BROKEN || echo ok
sudo ls -ld /var/lib/ccswitch-codex-auth          # expect drwx------ ccswitch-auth

# No runner may hold a credential cache. Expect NO output, on every runner.
find /var/lib/viberpit/state/ccswitch/codex -name auth.json 2>/dev/null
```

---

## 2. The authority PKI — a separate trust domain

The authority does **not** reuse the session daemon's CA, server key, or client
key. `server/codex/auth_endpoint.py:assert_distinct_from_sessiond` makes a shared
private key a startup failure rather than a review comment, because the
misconfiguration is invisible once running: the handshake succeeds and the two
trust domains are silently merged.

Certificate common names carry the role and the per-route ACL is built on them:

| CN | May do |
|---|---|
| `codex-edge` | administer logins, push the authorization sync |
| `codex-runner:<host>` | request an envelope or a refresh — nothing else |

A certificate presenting both identities is rejected outright.

### Mint the authority CA and server cert (once)

Run on the authority host. Keep this CA in its own directory — it must not land in
`/etc/viberpit/tls/`, which is the session-daemon fleet CA.

```bash
sudo install -d -m 0700 -o root -g root /etc/viberpit/codex-auth-ca
cd /etc/viberpit/codex-auth-ca
sudo umask 077

# 1. The authority's own CA (signs the SERVER cert runners verify).
sudo openssl req -x509 -newkey rsa:2048 -nodes -keyout server-ca.key -out server-ca.pem \
     -subj "/CN=ccswitch-codex-auth-ca" -days 3650

# 2. A second CA that signs CLIENT certs. Separate from the server CA so that
#    possession of the cert runners trust does not let anyone mint a client
#    identity the authority accepts.
sudo openssl req -x509 -newkey rsa:2048 -nodes -keyout client-ca.key -out client-ca.pem \
     -subj "/CN=ccswitch-codex-auth-client-ca" -days 3650

# 3. The listener's server cert. The SAN must match how callers REACH it
#    (CCSWITCH_CODEX_AUTHORITY_ENDPOINT) — use the tailnet name, never a LAN name.
sudo openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
     -subj "/CN=ccswitch-codex-auth"
printf 'subjectAltName=DNS:dobby.tail0dd570.ts.net,DNS:dobby\n' | sudo tee san.cnf >/dev/null
sudo openssl x509 -req -in server.csr -CA server-ca.pem -CAkey server-ca.key \
     -CAcreateserial -out server.pem -days 825 -extfile san.cnf

sudo install -d -m 0750 -o ccswitch-auth -g ccswitch-auth /etc/viberpit/codex-auth
sudo install -m 0640 -o ccswitch-auth -g ccswitch-auth server.pem  /etc/viberpit/codex-auth/
sudo install -m 0600 -o ccswitch-auth -g ccswitch-auth server.key  /etc/viberpit/codex-auth/
sudo install -m 0640 -o ccswitch-auth -g ccswitch-auth client-ca.pem /etc/viberpit/codex-auth/
```

### Mint a client identity (per caller)

```bash
# ROLE is exactly the CN the allowlist pins: codex-edge, or codex-runner:<host>.
ROLE=codex-runner:rocky
cd /etc/viberpit/codex-auth-ca
sudo openssl req -newkey rsa:2048 -nodes -keyout "${ROLE//:/-}.key" -out "${ROLE//:/-}.csr" \
     -subj "/CN=${ROLE}"
sudo openssl x509 -req -in "${ROLE//:/-}.csr" -CA client-ca.pem -CAkey client-ca.key \
     -CAcreateserial -out "${ROLE//:/-}.pem" -days 825
```

Copy `<role>.pem`, `<role>.key` (**0600, owned by the caller's user**) and
`server-ca.pem` to the caller, then add the CN to
`CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST` and restart the authority. An empty
allowlist is a startup error here, not "permissive" — this is the one service that
hands out ChatGPT access tokens.

`python -m server.codex.doctor` on the caller checks the file mode, the validity
window, and reports the CN it will actually present, which is the thing most often
wrong when onboarding.

### Rotation

Client and server certs rotate independently of the session-daemon PKI and of each
other. `mtls_identity` warns 14 days before `notAfter`, so rotate on that signal:

1. Mint the replacement with the **existing** CA (above). A fresh CA is not
   trusted by the other side and turns a rotation into an outage.
2. Install it on the caller, `systemctl restart` that caller only.
3. `python -m server.codex.doctor` → `mtls_identity: valid`.

Rotating the **server** cert needs an authority restart, which is cheap: it holds
no sessions, and the persisted generation counter means a restart cannot re-mint a
number an in-flight runner is still fencing against. Rotating a **CA** is a
two-phase job — distribute the new CA to every verifier first, then swap the leaf.

---

## 3. Onboarding a runner to Codex

Prerequisite: the host is already a working session runner
(`docs/runbooks/onboarding-a-session-runner.md`). Codex is strictly additive; a
runner with none of this stays healthy and keeps taking Claude sessions.

```bash
# 1. Install the pinned Codex CLI. Fails closed unless the artefact's digest
#    matches scripts/codex-cli.sha256 AND `codex --version` matches
#    server/codex/schema.py:PINNED_CLI_VERSION.
bash scripts/fetch-codex.sh

# 2. Client identity for this host (mint on the authority host, § 2 above).
sudo install -d -m 0750 -o tony -g tony /etc/viberpit/codex-auth
# copy codex-runner-<host>.pem/.key (0600, owned by tony) and server-ca.pem here

# 3. Daemon env — /etc/viberpit/viberpit.env on this host
CCSWITCH_CODEX_BIN=/home/tony/.local/lib/ccswitch/codex
CCSWITCH_CODEX_AUTHORITY_ENDPOINT=https://dobby.tail0dd570.ts.net:8442
CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT=/etc/viberpit/codex-auth/codex-runner-<host>.pem
CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY=/etc/viberpit/codex-auth/codex-runner-<host>.key
CCSWITCH_CODEX_AUTHORITY_CLIENT_CA=/etc/viberpit/codex-auth/server-ca.pem
# Leave BOTH launch flags false until the launch bar in § 8 has passed.
CCSWITCH_CODEX_ENABLED=false
CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED=false

# 4. Add this host's CN to the authority allowlist and restart the AUTHORITY.
# 5. Check the whole boundary before restarting the daemon:
sudo -u tony CCSWITCH_CODEX_ENABLED=true CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED=true \
     /home/tony/GitHub/viberpit/.venv/bin/python -m server.codex.doctor
```

The doctor exits 0 when nothing failed and prints one JSON object with six checks:

| check | what a FAIL means |
|---|---|
| `external_auth` | Codex is on but the external-token API is off — no session can authenticate |
| `binary` | the typed `probe_availability` reason: `binary_not_found`, `unsupported_version`, … |
| `runtime_home` | `network_filesystem`, `filesystem_undeterminable`, `symlinked_path`, `foreign_owner` |
| `authority` | `unconfigured` (nothing to reach) vs `unreachable` (something that did not answer) |
| `mtls_identity` | `certificate_expired`, `key_permissions_too_open`, `tls_material_unusable` |
| `manifest` | `openai_account_misrouted` — an OpenAI account not routed at the authority |

**Restarting the session daemon touches every live session.** Do it when the
Sessions page shows nothing mid-turn; it drains in-flight turns on SIGTERM first
(up to ~3 min, ~10 min mid-compaction), so the command blocking is expected.

---

## 4. Adding an account and reauth

Both go through the edge API; neither ever puts a credential on a runner.

```bash
# Start a ChatGPT device login (202 + userCode/verificationUri to visit)
curl -sS -X POST https://<edge>/api/accounts/add/openai \
     -H 'content-type: application/json' -d '{"name":"chatgpt-main"}'

# Poll it
curl -sS https://<edge>/api/accounts/add/openai
# Abandon it
curl -sS -X POST https://<edge>/api/accounts/add/openai/cancel
```

The manifest entry is written **only after** the login succeeds. For an OpenAI
account that entry is the *only* proof the account exists — there is no local
credential artefact to look for, by design — so it must say
`runnerAuthMode: chatgpt_external_tokens` and `credentialAuthority: codex_auth_service`.
The doctor's `manifest` check fails on any OpenAI entry that does not, because a
runner would otherwise go looking for a local credential file for it.

**Reauth** (an expired or revoked refresh token) is the same device flow against
the existing account name. Runners need no action: the generation counter bumps,
and the next envelope request picks up the new credential. Watch
`codex_auth_generation{account}` — it should advance once, not once per runner.

### Revoking

Order matters and the API enforces it: **revoke at the authority → fan
invalidation out to every runner → write the manifest.** A partial fan-out returns
HTTP 502 with `degraded: true` rather than reporting the account disabled, because
some host may still be running an app-server with a live envelope. Retry until
clean.

---

## 5. Backup and restore

Back up **`/var/lib/ccswitch-codex-auth/state/`** and treat
**`/var/lib/ccswitch-codex-auth/accounts/`** as a secret.

* `state/<account>.json` — the non-secret generation counter and durable
  revocation bit. It lives *outside* the managed home precisely so `codex logout`
  (which empties the home) cannot rewind either fence. **This is the part worth
  backing up:** losing it rewinds the anti-rollback counter, and a runner still
  fencing on a higher generation will reject envelopes until it is bumped past.
* `accounts/<account>/auth.json` — the refresh token. Restoring it elsewhere
  *is* a credential copy. Prefer re-running the device login over restoring it.

```bash
# Backup (root only; keeps mode/owner). Store encrypted, off-host.
sudo tar -C /var/lib/ccswitch-codex-auth -cpf codex-auth-state.tar state/

# Restore
sudo systemctl stop viberpit-codex-auth.service
sudo tar -C /var/lib/ccswitch-codex-auth -xpf codex-auth-state.tar
sudo chown -R ccswitch-auth:ccswitch-auth /var/lib/ccswitch-codex-auth/state
sudo systemctl start viberpit-codex-auth.service
```

There is **no failover in v1.** A second authority sharing the state directory
would be a second writer of the generation counter, so the second start takes an
exclusive lock or exits 3. Do not "restore" onto a second host while the first is
running.

---

## 6. Upgrading the Codex CLI

The CLI version and the app-server JSON-schema contract are **one pin**
(`server/codex/schema.py`: `PINNED_CLI_VERSION` + `PINNED_CONTRACT_SHA256`). A
version bump that changes the schema is a code change, not an ops change.

1. On a trusted host, fetch the new release and regenerate the contract fixtures:
   ```bash
   python scripts/refresh_codex_contract_fixtures.py
   ```
2. Land a PR updating both pins and the regenerated fixtures under
   `server/tests/fixtures/codex_app_server/<version>/`. Review the fixture diff —
   that is the reviewable record of what moved at the boundary.
3. Record the new artefact digest and commit it:
   ```bash
   bash scripts/fetch-codex.sh --record
   ```
4. Roll it out **one runner at a time**, checking `python -m server.codex.doctor`
   on each before moving on.

A runner ahead of the edge is caught rather than tolerated: the edge compares the
runner's observed `contractSha256` against its own pin and reports
`contract_hash_mismatch` with `dispatchable: false`.

`CCSWITCH_CODEX_SUPPORTED_VERSIONS` widens the accepted **version label** only. It
can never waive a schema-contract change, and a binary it admits is logged as
`codex.process.version_drift_accepted` and surfaced by the doctor as
`version_drift_accepted`. It is a bridge across a rollout, not a place to live.

---

## 7. Rollback and the kill switch

Codex is **fail-closed by construction**: both launch flags default false, and
absence or incompatibility disables Codex *only*. Nothing here touches Claude or
Copilot sessions.

```bash
# Kill switch — one runner. Live Codex sessions end; Claude sessions are untouched.
# In /etc/viberpit/viberpit.env on that host:
CCSWITCH_CODEX_ENABLED=false
sudo systemctl restart viberpit-daemon.service     # drains in-flight turns first

# Kill switch — whole fleet, without touching any daemon. Stopping the authority
# means no runner can obtain an envelope, so no Codex session can start or refresh.
sudo systemctl stop viberpit-codex-auth.service

# Rollback the binary: reinstall the previous pin from a checkout at that commit.
git -C <checkout> checkout <previous-sha> -- server/codex/schema.py scripts/codex-cli.sha256
bash scripts/fetch-codex.sh
```

Turning `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` off while `CCSWITCH_CODEX_ENABLED`
stays on is **not** a supported state — it advertises an engine that cannot
authenticate. The doctor reports it as a FAIL, and `probe_availability` disables
Codex anyway. Turn off `CCSWITCH_CODEX_ENABLED`.

---

## 8. What to watch

Metrics are defined in `server/obs/metrics.py`. None of them carries a native
thread id, prompt, command, or token — the label sets never collect one.

| Series | Watch for |
|---|---|
| `codex_auth_requests_total{method,outcome}` | `outcome=unavailable` is an outage; a typed code (`unknown_account`, `sync_lease_lapsed`) is a *refusal* and needs a different fix |
| `codex_auth_generation{account}` | should advance **once** per reauth, not once per runner — a 401 storm must coalesce into one refresh |
| `codex_request_ms{method,outcome}` | app-server round-trip; a rising `error` count precedes crashes |
| `codex_runner_restarts_total{account,outcome}` | `gave_up` means the restart budget is exhausted and that account is down |
| `codex_threads_active` / `codex_turns_active` | threads climbing with no turns = the idle reaper is not firing |
| `codex_approval_ms{outcome}` | `unrouted` must stay at zero: an approval nobody owns is never answered |
| `codex_quota_last_read_timestamp_s{account}` | age = `now - value`; a frozen value means quota reads are failing |

Structured logs: `codex.doctor.report` (once per daemon boot),
`codex.manager.runner_crashed` / `runner_restarted`, `codex.manager.unrouted_approval`,
`codex.process.version_drift_accepted`.

---

## 9. Troubleshooting

"Codex isn't offered" has its own decision list in `deploy/README.md` — start
there, it distinguishes an edge problem from a runner problem in one step.

| Symptom | Cause | Fix |
|---|---|---|
| Authority exits 3 at start | another authority holds the state lock | find and stop it; there is no failover |
| Authority refuses to start, complains about a key | `CCSWITCH_CODEX_AUTHORITY_*_KEY` is a session-daemon key | mint authority-specific material (§ 2) |
| `doctor: mtls_identity/tls_material_unusable` | missing, unreadable, or shared-with-sessiond material | § 2 |
| `doctor: authority/unreachable` | listener down, SAN mismatch, or CN not in the allowlist | check the SAN matches `CCSWITCH_CODEX_AUTHORITY_ENDPOINT` |
| `doctor: runtime_home/network_filesystem` | `XDG_STATE_HOME` resolves onto a FUSE/NFS mount | confirm the unit's `Environment=XDG_STATE_HOME=%S/viberpit/state` is installed and `daemon-reload`ed |
| Runner start refused, "credential cache appeared" | a runner is holding `auth.json` | do not work around it — this is the invariant; find what wrote it |
| Envelopes rejected after a restore | the generation counter was rewound | § 5; bump past the generation runners are fencing on |
| `sync_lease_lapsed` | the edge stopped renewing the authorization projection | `CCSWITCH_CODEX_AUTH_SYNC_INTERVAL_S` must be **below** `_LEASE_S` |

---

## Related

* `deploy/README.md` § "ChatGPT credential authority" — architecture, install, env
* `docs/runbooks/onboarding-a-session-runner.md` — the non-Codex prerequisites
* `docs/runbooks/rocky-session-host.md` — the remote-runner specifics
* `docs/product/features/openai-codex-integration.md` — what the feature is
* `docs/superpowers/plans/2026-09-04-native-openai-codex-integration.md` — the plan
