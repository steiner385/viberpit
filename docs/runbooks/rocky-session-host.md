# Rocky session-host runbook

Operational reference for the cross-host session fleet: `dobby` (edge + database + credentials)
and `rocky` (session daemon — runs every live `claude` process). This document describes what was
**actually built** during the 2026-08-20 cutover (sub-project 2 of
`docs/superpowers/specs/2026-08-20-multi-host-session-fleet-design.md`), which differs from that
design's original plan in one load-bearing way: **decision 7 was reversed mid-execution — rocky
joined the tailnet.** The original plan routed dobby↔rocky over the LAN with a narrow Postgres
`/32` rule; that was rejected because the PostgreSQL wire protocol is cleartext (`scram-sha-256`
protects the password, not the data), so every session transcript and prompt would have crossed the
LAN readable. See `.superpowers/sdd/2026-08-20-rocky-session-host/progress.md` for the full
decision record.

If you find a command below that doesn't match reality, the live hosts are the source of truth —
this doc, not the other way around.

## 1. Architecture summary

| | dobby (master) | rocky (worker) |
|---|---|---|
| Role | web edge, Postgres, canonical credentials | session daemon — owns every live `claude` process |
| Identity | tailnet `dobby`, `100.102.11.121` | tailnet `rocky.tail0dd570.ts.net`, `100.69.70.109` |
| `ccswitch.service` (edge) | active | not installed |
| `ccswitch-daemon.service` | **stopped + disabled** | active + enabled |
| Postgres | binds `localhost` + `100.102.11.121` only — **no LAN listener** | client only (asyncpg over tailnet) |
| `~/.claude-accounts` (account store, credentials, ledger) | canonical | sshfs mount from dobby |
| `~/.claude` (shared skills/commands/hooks/plugins/projects) | canonical | sshfs mount from dobby |

```
                         tailnet (WireGuard)
                               │
  ┌─────────────────── dobby (100.102.11.121) ────────────────────┐
  │  ccswitch.service (edge, :8787)                                │
  │    │ CCSWITCH_SESSIOND_ENDPOINT=https://rocky.tail0dd570.ts.net:9443
  │    │ client cert/key/CA in /etc/ccswitch/tls/                  │
  │  Postgres :5432 — listen_addresses = localhost,100.102.11.121  │
  │    pg_hba: host ccswitch ccswitch 100.69.70.109/32 scram-sha-256
  │  ccswitch-daemon.service — STOPPED, DISABLED (split-brain guard)
  │  ~/.claude-accounts, ~/.claude — canonical, exported via sshfs │
  └─────────────────────────────┬───────────────────────────────────┘
                                 │ mTLS (mutual, CERT_REQUIRED)
  ┌────────────── rocky (100.69.70.109 / rocky.tail0dd570.ts.net) ─┐
  │  ccswitch-daemon.service — https://…:9443                     │
  │    claude PTYs, MCP fleet, Xvfb (browser reauth)               │
  │    Postgres client → dobby, over the tailnet                   │
  │  ~/.claude-accounts  ← sshfs from dobby (creds, ledger)         │
  │  ~/.claude           ← sshfs from dobby (skills/commands/hooks) │
  │  ~/.claude.local-preserved — rocky's pre-existing ~/.claude,    │
  │    kept but no longer live                                     │
  └───────────────────────────────────────────────────────────────┘
```

**Why the tailnet, not the LAN:** `rocky` resolves to a LAN address via mDNS (pre-existing, outside
ccswitch's control). A cert or endpoint minted for `rocky.lan` describes a path the edge never
actually uses once it's pointed at the tailnet name, and produces the exact "certificate does not
match the server name" failure this doc's troubleshooting section covers. Always use the tailnet
identity (`rocky.tail0dd570.ts.net` / `100.69.70.109`), never the bare `rocky` hostname or a
`.lan` name, anywhere in ccswitch config.

## 2. How to tell where a session is running

```bash
# On rocky — expect ccswitch-managed claude processes here
ssh tony@rocky 'pgrep -af "claude_agent_sdk/_bundled/claude"'

# On dobby — expect NOTHING
pgrep -af "claude_agent_sdk/_bundled/claude"
```

**Do not use a bare `pgrep claude`** to check this. The user's own interactive `claude` CLI running
on dobby's desktop is unrelated to ccswitch and will still show up in a bare `pgrep claude` on
either host — only the `_bundled` path in the process command line identifies a ccswitch-managed
session. If `_bundled/claude` ever shows up on dobby, the edge is talking to a local (in-process or
stale local-daemon) orchestrator instead of rocky — treat as an incident (see §9).

## 3. mTLS enforcement check

Run from dobby, using the client material at `/etc/ccswitch/tls/`:

```bash
# WITHOUT a client cert — must FAIL
curl -sS --cacert /etc/ccswitch/tls/ca.pem https://rocky.tail0dd570.ts.net:9443/info; echo "exit=$?"

# WITH the client cert — must return the daemon's /info JSON
curl -sS --cacert /etc/ccswitch/tls/ca.pem \
     --cert /etc/ccswitch/tls/client.pem --key /etc/ccswitch/tls/client.key \
     https://rocky.tail0dd570.ts.net:9443/info; echo
```

Expected:
- No-cert case: a non-zero curl exit and no JSON body (observed as `curl: (52) Empty reply from
  server`; depending on curl/OpenSSL version this can instead show as `(56) Recv failure:
  Connection reset by peer` — either is a correctly-rejected handshake).
- With-cert case: `{"sha":"<short-sha>","protocol":1,"epoch":"..."}`.

**If the no-cert case ever returns JSON (succeeds), mTLS is not being enforced on rocky's daemon.
Treat this as a security incident, not a config quirk** — stop and investigate
`CCSWITCH_SESSIOND_TLS_CA` / `CCSWITCH_SESSIOND_CLIENT_ALLOWLIST` on rocky before doing anything
else with the daemon.

## 4. Mount health

Rocky depends on two sshfs mounts from dobby, both read continuously by the daemon:

```bash
ssh tony@rocky 'findmnt -t fuse.sshfs'
```

Expect both:
```
TARGET                      SOURCE                                          FSTYPE     OPTIONS
/home/tony/.claude-accounts tony@100.102.11.121:/home/tony/.claude-accounts fuse.sshfs rw,...
/home/tony/.claude          tony@100.102.11.121:/home/tony/.claude          fuse.sshfs rw,...
```

If either is missing from `findmnt`, or a read against it hangs/errors (`ls
~/.claude-accounts/cfg` on rocky), the mount has dropped:

| Mount dropped | What breaks | User-visible symptom |
|---|---|---|
| `~/.claude-accounts` | Credentials, ledger, account config unreadable | New sessions fail with `accounts_need_reauth`-style errors even for accounts that are actually fine; existing sessions may error mid-turn if they touch the store |
| `~/.claude` | Shared skills/commands/hooks/plugins gone | Sessions launch but silently run with rocky's stale/empty local skillset instead of dobby's — no error, just wrong behavior (this is the exact bug the mount was added to fix; see `.superpowers/sdd/2026-08-20-rocky-session-host/cross-host-path-audit.md` Finding 1) |

**Remount:**

```bash
ssh tony@rocky 'sudo umount -l ~/.claude-accounts 2>/dev/null; \
  sshfs -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 \
    tony@100.102.11.121:/home/tony/.claude-accounts ~/.claude-accounts'
ssh tony@rocky 'sudo umount -l ~/.claude 2>/dev/null; \
  sshfs -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 \
    tony@100.102.11.121:/home/tony/.claude ~/.claude'
ssh tony@rocky 'findmnt -t fuse.sshfs'   # confirm both are back
```

After remounting, restart the daemon so any process that opened a file handle across the drop picks
up a fresh view: `ssh tony@rocky 'sudo systemctl restart ccswitch-daemon.service'`.

The `reconnect` sshfs option handles a transient network blip automatically (rocky's daemon unit
also declares `Requires=tailscaled.service`, so it won't even start before the tailnet is up); the
manual remount above is for the case where the mount has actually gone away (e.g. dobby rebooted,
or the sshfs process itself died) rather than just stalled.

## 5. Deploy

`server/deploy/steps.py::restart_units` restarts the daemon over SSH instead of locally when
`CCSWITCH_DAEMON_SSH_HOST` is set (this mechanism ships on `feat/deploy-remote-daemon-restart`
(PR #708) — confirm it's merged into the checkout you're deploying from before relying on it).
When set, it **bypasses the local `daemon_enabled` guard for the remote restart** — dobby's local
`ccswitch-daemon.service` is deliberately disabled once the daemon has moved to rocky, so gating on
that flag would silently restart nothing. `daemon_changed` still gates it.

**The deploy also updates rocky's checkout, not just its unit.** Restarting alone relaunches the
same stale commit: rocky has its OWN clone at `/home/tony/GitHub/ccswitch`, and until 2026-08-21
nothing in the deploy fetched or checked out there. Deploying #715 therefore gave dobby's edge the
`cloned_repos` RPC *caller* while rocky's daemon stayed on `f310aa6` without the *handler*, and
every repo listing returned `409 {"detail": "cloned_repos"}` — the New Session dropdown showed
"— could not load repos —". Rocky reported "0 commits behind" the whole time, because its
`origin/master` ref was itself stale.

`restart_units` now runs, in order:

```
ssh <host> git -C <repo> fetch --quiet origin
ssh <host> git -C <repo> merge --ff-only <deployed-sha>
ssh <host> sudo systemctl restart ccswitch-daemon.service
```

Fast-forward only — a diverged remote checkout aborts the deploy rather than losing local commits.
This requires **`CCSWITCH_DAEMON_SSH_REPO`**, the checkout path on the remote host:

```
CCSWITCH_DAEMON_SSH_REPO=/home/tony/GitHub/ccswitch
```

If `CCSWITCH_DAEMON_SSH_HOST` is set and this is missing (or is not a safe absolute path), the
deploy raises `DeployError` **before any command runs**, including the edge restart. That is
deliberate: a deploy that cannot update the remote daemon must fail loudly rather than restart it
into staleness, which is the failure this whole section exists to describe.

### A hotfixed remote host cannot fast-forward

If you ever patch rocky directly — a branch, a cherry-pick, a copied file — the next
deploy will **fail**, and that is correct. This repo squash-merges, so the branch you
hotfixed with is *not* an ancestor of `master` even after its PR lands, and
`git merge --ff-only <deployed-sha>` refuses rather than discard what looks like
unmerged work.

Recovery is deliberate, not automatic. Verify master actually carries the fix **by
content**, not by trusting that the PR merged:

```bash
# example: confirm the fix you hotfixed with is really in master
git show origin/master:server/sessions/orchestrator.py | grep -c 'def project_dir_name'
git show origin/master:src/ccswitch/ledger.py            | grep -c 'os.replace'
```

Then reset the host and restart it:

```bash
ssh tony@rocky 'cd /home/tony/GitHub/ccswitch   && git fetch -q origin && git checkout -q master   && git reset --hard origin/master && git branch -D <hotfix-branch>'
ssh tony@rocky 'sudo systemctl restart ccswitch-daemon'
```

Check for local work first (`git log --branches --not --remotes`, `git stash list`) —
`reset --hard` discards it, and the whole point of `--ff-only` in the deploy is that it
will not make that decision for you.

**Version skew is measured between hosts, not against each host's own upstream.** To check:

```bash
# edge
git -C /home/tony/GitHub/cc-account-switcher-deploy rev-parse --short HEAD
# daemon (authoritative — the RUNNING code, not the checkout)
curl -s --unix-socket /run/ccswitch/deploy.sock http://localhost/status | jq -r .pending.liveSha
ssh tony@rocky 'git -C /home/tony/GitHub/ccswitch rev-parse --short HEAD'
```

```bash
run(["ssh", daemon_host, "sudo", "systemctl", "restart", "ccswitch-daemon.service"], ...)
```

`CCSWITCH_DAEMON_SSH_HOST` is validated against `^[A-Za-z0-9][A-Za-z0-9_.-]*(@[A-Za-z0-9][A-Za-z0-9_.-]*)?$`
before it's used to build the ssh argv — a value starting with `-` (ssh-flag injection) raises
`DeployError` before any command runs, including the local edge restart. Live value on dobby:

```
CCSWITCH_DAEMON_SSH_HOST=tony@rocky
```

This lives in `/etc/ccswitch/ccswitch.env`, which `ccswitch-deploy-controller.service` sources via
its unit's `EnvironmentFile=`. **A manual `python -m server.deploy --once` invocation from an
interactive shell does NOT source that file** — systemd's `EnvironmentFile=` is unit-scoped, not a
shell-sourceable file. For a manual deploy, export it first:

```bash
set -a; source /etc/ccswitch/ccswitch.env; set +a
.venv/bin/python -m server.deploy --once
```

Without that export, a manual deploy silently falls back to `restart_units`'s local-only branch,
which restarts `ccswitch.service` but skips the daemon entirely (dobby's unit is disabled) — rocky
keeps running whatever code it was already running.

**Deploy order when doing it by hand:** daemon (rocky) first, then edge (dobby). This mirrors
`restart_units`'s own ordering (issue #190 — a browser reconnect-flush race) and matters more in the
cross-host case: a new edge build talking to an old daemon can silently degrade — e.g. an RPC the
new edge expects but the old daemon doesn't implement yet fails or is ignored rather than
erroring loudly. Restart the daemon, confirm it's healthy (§3's with-cert curl), *then* restart the
edge.

## 6. Certificate renewal

Server/client certs are valid **825 days**, the CAs **3650 days**. The SAN embedded in the server
cert must stay the tailnet identity, never `rocky.lan`.

**Do NOT re-run `scripts/ccswitch-mint-mtls.sh` to renew** — it mints a brand-new CA (and since
issue #841 refuses to run when a fleet CA already exists). Renew by signing a fresh cert with the
existing signing CA per the onboarding runbook, Step 3 Option A
(`docs/runbooks/onboarding-a-session-runner.md`) — that runbook's CA table is the source of truth.
Summary of the fleet CA state (since 2026-08-30, issue #841):

- **Server-cert renewals** (rocky, dobby, any runner): sign with `ca2.key`
  (`/etc/ccswitch/tls/ca2.key` on dobby, root-only). The edge verifies server certs against
  `ca-bundle.pem` (original `ca.pem` + `ca2.pem`), so old and new certs coexist.
- **Client-cert renewal** (`CN=edge`): every daemon verifies it against its
  `CCSWITCH_SESSIOND_TLS_CA` — currently the ORIGINAL `ca.pem`. Either sign with the original CA
  key, which is intact at `/mnt/hdd-storage/ccswitch-ca/ca.key` on dobby (a **root-only**
  directory: a non-root search reports permission denied/nothing — it is NOT lost), or first point
  every daemon's `CCSWITCH_SESSIOND_TLS_CA` at a ca.pem+ca2.pem bundle and sign with ca2.

Distribute renewed material and clean up:

```bash
# server material → the runner being renewed (e.g. rocky)
scp /tmp/mtls-rocky/{server.pem,server.key} tony@rocky:/etc/ccswitch/tls/
# renewed client material → dobby (edge)
sudo cp /tmp/mtls-edge/{client.pem,client.key} /etc/ccswitch/tls/
sudo chmod 600 /etc/ccswitch/tls/*.key
rm -rf /tmp/mtls-*   # never leave cert material in /tmp
```

Then restart both daemons that read TLS material: `ssh tony@rocky 'sudo systemctl restart
ccswitch-daemon.service'`, then `sudo systemctl restart ccswitch.service` on dobby (the edge holds
the client cert). Confirm with §3's mTLS check afterward.

Do not commit `.pem`/`.key` files. Only `scripts/ccswitch-mint-mtls.sh` itself belongs in the repo.

## 7. Rollback to dobby

Full sequence to move the daemon back onto dobby (e.g. rocky is down, or the cross-host setup needs
to be backed out). **Stop rocky's daemon first — two daemons must never run against the same
database at once** (the 2026-06-23 "Ready while working" split-brain).

```bash
# 1. Stop rocky's daemon so it cannot compete with dobby's once re-enabled
ssh tony@rocky 'sudo systemctl stop ccswitch-daemon.service'

# 2. Re-enable and start dobby's local daemon
sudo systemctl enable --now ccswitch-daemon.service
systemctl is-active ccswitch-daemon.service   # expect: active

# 3. Point the edge back at the local Unix socket — edit /etc/ccswitch/ccswitch.env:
#      comment out CCSWITCH_SESSIOND_ENDPOINT and the CCSWITCH_SESSIOND_CLIENT_* lines
#      uncomment (or add) CCSWITCH_SESSIOND_SOCKET=/run/ccswitch/sessiond.sock
#      also comment out CCSWITCH_DAEMON_SSH_HOST so future deploys restart the local unit
sudo -e /etc/ccswitch/ccswitch.env

# 4. Also flip the systemd drop-in that pins the edge's daemon endpoint back to the socket.
#    A pre-rocky copy is preserved for exactly this: restore it over the live one.
sudo cp /etc/systemd/system/ccswitch.service.d/remote.conf.pre-rocky \
        /etc/systemd/system/ccswitch.service.d/remote.conf
sudo systemctl daemon-reload

# 5. Restart the edge
sudo systemctl restart ccswitch.service

# 6. Verify
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/health   # expect 200
pgrep -af "claude_agent_sdk/_bundled/claude"   # start a test session, confirm it's on dobby now
```

To go back to rocky afterward, reverse steps 2-4 (disable+stop dobby's daemon, restore the
`CCSWITCH_SESSIOND_ENDPOINT`/client-cert lines and `CCSWITCH_DAEMON_SSH_HOST`, restore
`remote.conf`'s `Environment=CCSWITCH_SESSIOND_ENDPOINT=...`) and confirm with §2/§3 that sessions
land on rocky again and mTLS is enforced.

## 8. Known limitations

Per `.superpowers/sdd/2026-08-20-rocky-session-host/cross-host-path-audit.md`, these features still
assume the edge and daemon share a filesystem and were **not** fixed by the `~/.claude`/
`~/.claude-accounts` mounts, because they scan the edge host's local process table or filesystem
directly rather than going through a daemon RPC:

| Feature | What you will SEE | Not a bug — known gap |
|---|---|---|
| **Adopt session** (`GET /api/sessions/adoptable`) | The adoptable-candidates list is empty, even when an untracked `claude` process is genuinely running on rocky — because it scans the edge (dobby) host's `/proc`, not rocky's. | Yes — `list_adoptable` needs a daemon RPC mirroring the one `POST /adopt` already uses correctly. |
| **Archive-home migration** (Settings → change archive home) | Save reports success ("0 failed"), but no files actually move. Archived session directories live under rocky's `CCSWITCH_HOME`, and the migration's `shutil.move` runs on the edge (dobby), where the source paths never exist. | Yes — silent no-op, not a crash. |
| **Repo picker "cloned" badge** | Always shows as *not cloned*, even for repos genuinely cloned on rocky by an earlier session (`ensure_clone` is lazy/idempotent, so a session still works — the badge is just wrong). | Yes — cosmetic; the `.exists()` check runs against dobby's `.ccswitch/clones`, not rocky's. |

If you see anything **other** than the specific symptom described above for these three features
(e.g. a 500, a traceback, or a crash rather than an empty list / silent no-op / wrong badge), that is
a real bug — do not assume it's covered by this table. The audit lists five more findings (Library
subsystem read/write, attachments, account listing) that were already fixed by mounting `~/.claude`
and `~/.claude-accounts`, or by `fix/cross-host-transcript-replay` (merged) for transcript
view/search/export/live-attach — those should NOT reappear; if they do, that's a regression, report
it as one.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl: certificate does not match the server name` (or similar SAN error) from the edge to the daemon | Server cert SAN doesn't match `CCSWITCH_SESSIOND_ENDPOINT`'s host — e.g. cert minted for `rocky.lan` while the endpoint uses `rocky.tail0dd570.ts.net` | Re-mint with `scripts/ccswitch-mint-mtls.sh <outdir> rocky.tail0dd570.ts.net 100.69.70.109` (§6); verify the SAN before distributing |
| Edge refuses to attach to rocky's daemon / RPC calls fail after a deploy | Protocol-version mismatch — dobby and rocky are running different commits | Compare `curl .../info`'s `"protocol"` field against `python -c "from server.ipc import protocol; print(protocol.PROTOCOL_VERSION)"` on both hosts; they must match. Fix by deploying rocky to the same SHA as dobby (§5 — daemon first, then edge) |
| New sessions fail with reauth/credential errors for accounts that are otherwise fine; or sessions on rocky run with the wrong/old skillset | An sshfs mount dropped | §4 — check `findmnt -t fuse.sshfs` on rocky, remount both, restart the daemon |
| Sessions double-spawn, or the UI shows "Ready" for a session that's actually mid-turn | Both `ccswitch-daemon.service` units are active at once — split-brain over one database (the 2026-06-23 incident) | `systemctl is-active ccswitch-daemon.service` on **both** hosts — exactly one must be `active`. Stop and `disable` (not just stop) the one that shouldn't be running |
| Rocky's daemon won't start, or is up but can't reach Postgres | Tailscale is down on rocky — the unit declares `Requires=tailscaled.service`, and the DB is only reachable over the tailnet (no LAN listener) | `ssh tony@rocky 'systemctl status tailscaled.service ccswitch-daemon.service'`; fix tailscale first, the daemon unit will follow via its `Requires=` |
| Rocky's daemon logs Postgres auth/connection failures specifically (not a tailnet-down symptom) | `pg_hba.conf` on dobby only allows `100.69.70.109/32` for role `ccswitch`/db `ccswitch` — a password change, IP change (if rocky's tailnet IP is ever reassigned), or role/db typo will be rejected | Confirm rocky's current tailnet IP (`tailscale ip -4` on rocky) matches the `pg_hba` entry on dobby; confirm `CCSWITCH_DB_URL` in rocky's `/etc/ccswitch/ccswitch.env` matches the role/password/db name dobby's Postgres actually has |
| The mTLS no-cert curl (§3) unexpectedly returns JSON | mTLS is not enforced — `CCSWITCH_SESSIOND_TLS_CA` misconfigured, or the daemon isn't requiring client certs | Treat as a security incident (§3). Check rocky's `ccswitch.env` TLS vars and `server/ipc/tls.py`'s `CERT_REQUIRED` wiring before touching anything else |

## Reference: live config (as of the 2026-08-20 cutover)

Rocky's `/etc/ccswitch/ccswitch.env` (secrets redacted):
```
CCSWITCH_DB_URL=postgresql+asyncpg://ccswitch:<redacted>@100.102.11.121:5432/ccswitch
CCSWITCH_MASTER_KEY=<redacted>
CCSWITCH_SESSIOND_ENDPOINT=https://rocky.tail0dd570.ts.net:9443
CCSWITCH_SESSIOND_TLS_CERT=/etc/ccswitch/tls/server.pem
CCSWITCH_SESSIOND_TLS_KEY=/etc/ccswitch/tls/server.key
CCSWITCH_SESSIOND_TLS_CA=/etc/ccswitch/tls/ca.pem
CCSWITCH_SESSIOND_CLIENT_ALLOWLIST=edge
CCSWITCH_ENABLE_XVFB=true
```

Dobby's `/etc/ccswitch/ccswitch.env` additions for the cutover (secrets redacted):
```
CCSWITCH_SESSIOND_ENDPOINT=https://rocky.tail0dd570.ts.net:9443
CCSWITCH_SESSIOND_CLIENT_CERT=/etc/ccswitch/tls/client.pem
CCSWITCH_SESSIOND_CLIENT_KEY=/etc/ccswitch/tls/client.key
CCSWITCH_SESSIOND_CLIENT_CA=/etc/ccswitch/tls/ca.pem
CCSWITCH_DAEMON_SSH_HOST=tony@rocky
```

`deploy/ccswitch-daemon-remote.service` is installed on rocky as `/etc/systemd/system/ccswitch-daemon.service`
(dobby keeps its own `deploy/ccswitch-daemon.service` for the pre-cutover/rollback local case — the
two files are not interchangeable, don't install the remote unit on dobby or vice versa).
