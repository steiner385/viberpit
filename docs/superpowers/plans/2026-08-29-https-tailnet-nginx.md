# HTTPS via Tailnet (nginx + tailscale cert) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve CCswitch over a browser-trusted HTTPS cert at `https://dobby.tail0dd570.ts.net/` so browsers grant it a secure context (enabling `getUserMedia`, Web Speech API, etc.).

**Architecture:** `tailscale cert` provisions a Let's Encrypt cert for the tailnet FQDN; a dedicated nginx `server {}` block terminates TLS and proxies to `127.0.0.1:8787`. The `_whois_identity` fallback is fixed to read `X-Real-IP` (set by nginx) instead of `request.client.host` (always `127.0.0.1` behind nginx), so Tailscale identity resolution continues to work even without `Tailscale-User-Login` headers.

**Tech Stack:** Python / FastAPI / Starlette, nginx, systemd, `tailscale cert` (ACME via Tailscale)

**Spec:** `docs/superpowers/specs/2026-08-29-https-tailnet-nginx-design.md`

## Global Constraints

- The nginx vhost must only respond to SNI `dobby.tail0dd570.ts.net` — no catch-all.
- Cert files live in `/etc/ccswitch/` (root-owned, mode 600) alongside `ccswitch.env`.
- `Tailscale-User-Login` and `X-Tailscale-User-Login` headers MUST be stripped from client requests before forwarding — nginx overwrites them to empty.
- `X-Real-IP` MUST be set to `$remote_addr` by nginx (not from a client-supplied header).
- WebSocket proxy timeouts must be 86400s (xterm.js and VNC sessions stay open indefinitely).
- All Python changes must pass `mypy server/ --ignore-missing-imports` and `ruff check server/`.
- Run tests via the shared venv: `/home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/` with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `server/auth/identity.py` | Modify line 64 | Prefer `X-Real-IP` header over `request.client.host` in `_whois_identity` |
| `server/tests/test_auth.py` | Modify (append) | Tests for X-Real-IP whois fallback behaviour |
| `deploy/ccswitch-tailnet.nginx` | Create | nginx vhost: TLS termination, header strip, WS proxy to :8787 |
| `deploy/ccswitch-cert-renew.service` | Create | oneshot: `tailscale cert` then `nginx -s reload` |
| `deploy/ccswitch-cert-renew.timer` | Create | weekly trigger for cert renewal service |
| `specs/001-ccswitch-web/quickstart.md` | Modify lines 13-19 | Replace `tailscale serve` instructions with nginx + cert path |

---

## Task 1: Fix `_whois_identity` to use `X-Real-IP` when behind nginx

**Files:**
- Modify: `server/auth/identity.py:64`
- Test: `server/tests/test_auth.py` (append new tests)

**Interfaces:**
- Produces: `_whois_identity(request)` — unchanged signature, now reads `X-Real-IP` header first

- [ ] **Step 1: Write the failing tests**

Append these two tests to `server/tests/test_auth.py`:

```python
# ---------------------------------------------------------------------------
# X-Real-IP fallback (nginx reverse-proxy path)
# ---------------------------------------------------------------------------

import json as _json
import subprocess as _subprocess
from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_x_real_ip_used_for_whois_when_ts_header_absent(monkeypatch):
    """When Tailscale-User-Login is absent, X-Real-IP is passed to tailscale whois
    (not request.client.host, which is 127.0.0.1 behind nginx)."""
    whois_calls: list[str] = []

    def fake_whois(cmd, **kwargs):
        whois_calls.append(cmd[3])  # the IP argument
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = _json.dumps({"UserProfile": {"LoginName": "alice@example.com"}})
        return mock

    monkeypatch.setattr("server.auth.identity.subprocess.run", fake_whois)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/health",
            headers={"X-Real-IP": "100.64.0.1"},
        )
    assert response.status_code == 200
    assert whois_calls == ["100.64.0.1"], (
        f"whois should be called with X-Real-IP value, got {whois_calls}"
    )


@pytest.mark.asyncio
async def test_whois_falls_back_to_client_host_without_x_real_ip(monkeypatch):
    """Without X-Real-IP, whois falls back to request.client.host (direct-connection path)."""
    whois_calls: list[str] = []

    def fake_whois(cmd, **kwargs):
        whois_calls.append(cmd[3])
        mock = MagicMock()
        mock.returncode = 1  # whois fails → 401
        mock.stdout = ""
        return mock

    monkeypatch.setattr("server.auth.identity.subprocess.run", fake_whois)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 401
    # whois was attempted with whatever the ASGI transport sets as client.host —
    # the point is X-Real-IP was NOT used (header absent)
    assert len(whois_calls) == 1
    assert whois_calls[0] != "100.64.0.1"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/tony/GitHub/cc-account-switcher
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 \
  .venv/bin/python -m pytest server/tests/test_auth.py \
    -k "x_real_ip" -v
```

Expected: both tests FAIL (the first because whois is called with `request.client.host` instead of the header value).

- [ ] **Step 3: Implement the fix in `server/auth/identity.py` line 64**

Replace the single line:
```python
# Before
peer_ip = request.client.host if request.client else None
```

With:
```python
# After — prefer X-Real-IP set by the nginx reverse proxy; fall back to the
# direct TCP peer when running without a proxy (dev, direct tailscale serve).
peer_ip = (
    request.headers.get("X-Real-IP")
    or (request.client.host if request.client else None)
)
```

The full `_whois_identity` body after the change:

```python
def _whois_identity(request: Request | WebSocket) -> str | None:
    import json
    try:
        peer_ip = (
            request.headers.get("X-Real-IP")
            or (request.client.host if request.client else None)
        )
        if not peer_ip:
            return None
        result = subprocess.run(
            ["tailscale", "whois", "--json", peer_ip],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        login = (data.get("UserProfile") or {}).get("LoginName")
        return login or None
    except Exception:
        pass
    return None
```

- [ ] **Step 4: Run the full auth test suite to verify all tests pass**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 \
  .venv/bin/python -m pytest server/tests/test_auth.py -v
```

Expected: all tests PASS (including the two new ones).

- [ ] **Step 5: Run mypy and ruff**

```bash
.venv/bin/python -m mypy server/ --ignore-missing-imports
.venv/bin/ruff check server/
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add server/auth/identity.py server/tests/test_auth.py
git commit -m "fix(auth): prefer X-Real-IP over client.host in whois fallback

When nginx proxies requests over loopback, request.client.host is
127.0.0.1 — causing tailscale whois to fail. Read X-Real-IP first
(set unconditionally by nginx to \$remote_addr, the real Tailscale
peer IP) so identity resolution works behind the reverse proxy.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: nginx vhost config + cert renewal systemd units

**Files:**
- Create: `deploy/ccswitch-tailnet.nginx`
- Create: `deploy/ccswitch-cert-renew.service`
- Create: `deploy/ccswitch-cert-renew.timer`

**Interfaces:**
- Consumes: cert at `/etc/ccswitch/dobby.tail0dd570.ts.net.crt` and `.key` (provisioned in the one-time migration, not by these files)
- Produces: nginx vhost config deployable to `/etc/nginx/sites-available/ccswitch-tailnet`

- [ ] **Step 1: Create `deploy/ccswitch-tailnet.nginx`**

```nginx
# CCswitch HTTPS vhost — tailnet-only (dobby.tail0dd570.ts.net)
# Install: ln -sf /etc/nginx/sites-available/ccswitch-tailnet /etc/nginx/sites-enabled/
# Cert: provisioned by tailscale cert (see ccswitch-cert-renew.service)

server {
    listen 443 ssl;
    server_name dobby.tail0dd570.ts.net;

    ssl_certificate     /etc/ccswitch/dobby.tail0dd570.ts.net.crt;
    ssl_certificate_key /etc/ccswitch/dobby.tail0dd570.ts.net.key;

    # Anti-spoofing: strip any client-supplied Tailscale identity headers.
    # nginx sets these to empty before forwarding, so clients cannot inject
    # a fake identity. The app's X-Real-IP whois fallback relies on this.
    proxy_set_header Tailscale-User-Login   "";
    proxy_set_header X-Tailscale-User-Login "";

    # Pass the real Tailscale peer IP so the whois identity fallback works
    # when Tailscale-User-Login is not present.
    proxy_set_header X-Real-IP $remote_addr;

    # WebSocket upgrade — required for xterm.js terminal and VNC bridge.
    # Timeout set to 24 h; sessions stay open indefinitely.
    proxy_http_version 1.1;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;

    location / {
        proxy_pass http://127.0.0.1:8787;
    }
}
```

- [ ] **Step 2: Create `deploy/ccswitch-cert-renew.service`**

```ini
[Unit]
Description=Renew Tailscale TLS certificate for ccswitch
Documentation=https://tailscale.com/kb/1153/enabling-https/
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
# tailscale cert is idempotent: only fetches a new cert when near expiry.
# ExecStartPost only runs if ExecStart succeeds, so nginx is not reloaded
# with a bad or missing cert.
ExecStart=tailscale cert \
    --cert-file /etc/ccswitch/dobby.tail0dd570.ts.net.crt \
    --key-file  /etc/ccswitch/dobby.tail0dd570.ts.net.key \
    dobby.tail0dd570.ts.net
ExecStartPost=nginx -s reload
```

- [ ] **Step 3: Create `deploy/ccswitch-cert-renew.timer`**

```ini
[Unit]
Description=Weekly Tailscale cert renewal for ccswitch
Documentation=https://tailscale.com/kb/1153/enabling-https/

[Timer]
# Let's Encrypt certs are 90 days; weekly checks give ~12 renewal
# opportunities before expiry.
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Commit**

```bash
git add deploy/ccswitch-tailnet.nginx \
        deploy/ccswitch-cert-renew.service \
        deploy/ccswitch-cert-renew.timer
git commit -m "feat(deploy): nginx vhost + cert renewal units for HTTPS tailnet access

Adds a dedicated nginx server block for dobby.tail0dd570.ts.net with
a tailscale-cert-provisioned Let's Encrypt cert, proper WebSocket
headers for xterm.js/VNC, and identity-header stripping to prevent
spoofing. Paired with a weekly systemd timer that renews the cert and
reloads nginx.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: Update quickstart to reflect nginx setup

**Files:**
- Modify: `specs/001-ccswitch-web/quickstart.md:13-19`

**Interfaces:**
- Consumes: nothing from earlier tasks (doc-only)
- Produces: accurate run instructions for the nginx + tailscale cert hosting path

- [ ] **Step 1: Update the "Run" section in `specs/001-ccswitch-web/quickstart.md`**

Replace lines 13–19 (the `## Run (local / Tailscale)` section):

```markdown
## Run (local / Tailscale)
```bash
# backend (binds loopback; fronted by nginx for TLS termination)
CCSWITCH_MASTER_KEY=... python -m server.app          # serves API + built web assets
# dev frontend (optional, hot reload): cd web && npm run dev
```

Open `https://dobby.tail0dd570.ts.net/` on any tailnet device.

> **Production TLS setup (one-time, on dobby):**
> ```bash
> # 1. Provision cert
> sudo tailscale cert \
>     --cert-file /etc/ccswitch/dobby.tail0dd570.ts.net.crt \
>     --key-file  /etc/ccswitch/dobby.tail0dd570.ts.net.key \
>     dobby.tail0dd570.ts.net
>
> # 2. Install nginx vhost
> sudo cp deploy/ccswitch-tailnet.nginx /etc/nginx/sites-available/ccswitch-tailnet
> sudo ln -sf /etc/nginx/sites-available/ccswitch-tailnet /etc/nginx/sites-enabled/
> sudo nginx -t && sudo nginx -s reload
>
> # 3. Install cert renewal timer (runs weekly, auto-renews before expiry)
> sudo cp deploy/ccswitch-cert-renew.{service,timer} /etc/systemd/system/
> sudo systemctl daemon-reload
> sudo systemctl enable --now ccswitch-cert-renew.timer
> ```
```

- [ ] **Step 2: Commit**

```bash
git add specs/001-ccswitch-web/quickstart.md
git commit -m "docs(quickstart): replace tailscale serve with nginx + tailscale cert setup

The production host (dobby) uses nginx on port 443, so tailscale serve
cannot bind there. Update instructions to match the actual setup:
tailscale cert for the tailnet FQDN + a dedicated nginx vhost.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Final gate: open the PR

```bash
git fetch origin
git rebase origin/master
# Verify the full test suite is still green
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 \
  /home/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/ -q
# Open PR
gh pr create \
  --title "fix(auth): HTTPS via tailnet — nginx vhost + X-Real-IP whois fix" \
  --body "$(cat <<'EOF'
## Summary

- Fixes `NET::ERR_CERT_COMMON_NAME_INVALID` on `dobby.tail0dd570.ts.net` by adding a
  dedicated nginx vhost with a `tailscale cert`-provisioned Let's Encrypt cert.
- Fixes identity resolution behind nginx: `_whois_identity` now reads `X-Real-IP` (set by
  nginx to the real Tailscale peer IP) instead of `request.client.host` (`127.0.0.1`).
- Adds a weekly systemd timer to auto-renew the cert and reload nginx.
- Updates `quickstart.md` to document the nginx setup in place of `tailscale serve`.

## Test plan

- [ ] `pytest server/tests/test_auth.py` — all existing + two new X-Real-IP tests pass
- [ ] `mypy server/ --ignore-missing-imports` — no new errors
- [ ] `ruff check server/` — clean
- [ ] On dobby: run one-time migration steps from quickstart, visit
      `https://dobby.tail0dd570.ts.net/` — browser shows green padlock, no cert warning
- [ ] In browser devtools: `navigator.mediaDevices.getUserMedia({audio: true})` resolves
      (secure context confirmed)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```
