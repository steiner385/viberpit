# Deploy — ccswitch web (self-hosted, auto-deploy from master)

The server runs as a **systemd service** on the Tailscale host and is kept in sync
with `origin/master` by a **polling timer** (no inbound ports — GitHub can't reach
a `100.x` Tailscale address, so we poll rather than use a webhook).

## Components

| Unit | Role |
|------|------|
| `ccswitch.service` | Long-running uvicorn server (`server.app:app`), serves the API + built `web/dist`. |
| `scripts/auto-deploy.sh` | Poll → if behind `origin/master`: `git pull` → `npm run build` → **test gate** → `systemctl restart ccswitch`. No-op when current. |
| `ccswitch-deploy.service` | Oneshot wrapper that runs the script. |
| `ccswitch-deploy.timer` | Fires the deploy every 2 minutes. |

The **test gate** runs the frontend build and `pytest server/tests` before restarting;
if either fails the running service is left untouched, so a broken master never goes live.

## Runtime env (`/etc/ccswitch/ccswitch.env`, root-only, NOT committed)

Holds the secrets/config the server needs — `CCSWITCH_DB_URL`, `CCSWITCH_MASTER_KEY`,
`CCSWITCH_SESSION_SECRET`, `CCSWITCH_TAILNET_ALLOWLIST`, `CCSWITCH_HOST`, `CCSWITCH_PORT`
— plus `PATH`/`HOME` so the service can find `git`/`node`/`claude`. `chmod 600`, `root:root`.

## Install

```bash
sudo install -m644 deploy/ccswitch.service          /etc/systemd/system/
sudo install -m644 deploy/ccswitch-deploy.service   /etc/systemd/system/
sudo install -m644 deploy/ccswitch-deploy.timer     /etc/systemd/system/
chmod +x scripts/auto-deploy.sh
sudo systemctl daemon-reload
sudo systemctl enable --now ccswitch.service
sudo systemctl enable --now ccswitch-deploy.timer
```

## Operate

```bash
systemctl status ccswitch.service                 # server health
journalctl -u ccswitch.service -f                 # server logs
systemctl list-timers ccswitch-deploy.timer       # next poll
journalctl -u ccswitch-deploy.service -n 50       # deploy history
sudo systemctl start ccswitch-deploy.service      # force a deploy check now
```

## Notes

- **Backend dependency changes** (new Python deps) need a manual `uv sync`; the deploy
  script intentionally doesn't, to stay robust on a bandwidth-limited host. A missing dep
  surfaces as a red test gate (deploy aborts, old version stays live) rather than a crash.
- The deploy runs as `tony` and uses passwordless `sudo` only for `systemctl restart`.

## Session daemon (`ccswitch-daemon.service`)

`ccswitch-daemon.service` runs `python -m server.ipc` — the session daemon that will own
live `claude` PTY sessions once the edge switches to remote mode (A2.3). The daemon is
**not enabled until A2.3**; the unit file is shipped now so `auto-deploy.sh` can restart
it on hosts where it is enabled, without affecting CI or other hosts where it isn't.

The `RuntimeDirectory=ccswitch` directive creates `/run/ccswitch/` on each start; the
daemon's Unix socket lives at `/run/ccswitch/sessiond.sock`.

### Install

```bash
sudo install -m644 deploy/ccswitch-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
# Enable only when A2.3 is deployed (remote-mode edge):
# sudo systemctl enable --now ccswitch-daemon.service
```

The `auto-deploy.sh` script restarts `ccswitch-daemon.service` after a successful edge
restart, but only when `systemctl is-enabled ccswitch-daemon.service` succeeds — making
the block a complete no-op on CI and any host where the unit is not yet enabled.
