# HTTPS via Tailnet — nginx + tailscale cert

**Date:** 2026-08-29  
**Status:** Approved — ready for implementation planning  
**Goal:** Serve CCswitch over HTTPS at `https://dobby.tail0dd570.ts.net/` so modern browsers
grant it a secure context, enabling native microphone access (`getUserMedia`) and other
Secure Context APIs.

---

## Problem

nginx is already running on dobby and owns port 443. The current nginx vhost serves
`dev.kindash.com` and its TLS cert, causing `NET::ERR_CERT_COMMON_NAME_INVALID` when
browsers try `dobby.tail0dd570.ts.net`. The spec's original plan (`tailscale serve https`)
is incompatible with nginx owning port 443.

Additionally, `server/auth/identity.py::_whois_identity` uses `request.client.host` for
the `tailscale whois` fallback. With nginx proxying over loopback, that value is always
`127.0.0.1` — breaking identity resolution when `Tailscale-User-Login` is not present.

---

## Architecture

```
Browser (on tailnet)
  → nginx :443  [TLS: tailscale cert for dobby.tail0dd570.ts.net]
  → 127.0.0.1:8787  (uvicorn / FastAPI — unchanged)
```

The `tailscale cert` command provisions a Let's Encrypt certificate scoped to the tailnet
FQDN via Tailscale's ACME provider. The cert is browser-trusted on all platforms including
mobile. Clients outside the tailnet cannot resolve `dobby.tail0dd570.ts.net` (it is
Tailscale DNS-only), so no firewall changes are needed.

---

## Components

### 1. nginx vhost (`deploy/ccswitch-tailnet.nginx`)

A dedicated `server {}` block for `dobby.tail0dd570.ts.net`. Key properties:

- TLS cert/key from `/etc/ccswitch/dobby.tail0dd570.ts.net.{crt,key}`.
- Strips `Tailscale-User-Login` and `X-Tailscale-User-Login` from client requests to
  prevent spoofing (nginx overwrites them to empty before forwarding).
- Sets `X-Real-IP $remote_addr` so the FastAPI auth layer receives the real Tailscale
  peer IP for the `tailscale whois` fallback.
- WebSocket upgrade headers with 24-hour `proxy_read_timeout` / `proxy_send_timeout`
  (required for persistent xterm.js and VNC WebSocket connections).
- Single `location /` proxying all traffic to `http://127.0.0.1:8787`.

```nginx
server {
    listen 443 ssl;
    server_name dobby.tail0dd570.ts.net;

    ssl_certificate     /etc/ccswitch/dobby.tail0dd570.ts.net.crt;
    ssl_certificate_key /etc/ccswitch/dobby.tail0dd570.ts.net.key;

    # Anti-spoofing: strip any client-supplied identity headers
    proxy_set_header Tailscale-User-Login   "";
    proxy_set_header X-Tailscale-User-Login "";

    # Pass real Tailscale peer IP for whois fallback
    proxy_set_header X-Real-IP $remote_addr;

    # WebSocket support (xterm.js / VNC bridge)
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

### 2. Auth fix (`server/auth/identity.py`)

`_whois_identity` currently reads `request.client.host` (the direct TCP peer). With nginx
on loopback this is always `127.0.0.1`. Fix: prefer `X-Real-IP` header when present.

```python
# Before
peer_ip = request.client.host if request.client else None

# After
peer_ip = (
    request.headers.get("X-Real-IP")
    or (request.client.host if request.client else None)
)
```

`X-Real-IP` is set unconditionally by nginx to `$remote_addr` (the Tailscale peer IP),
so `tailscale whois` resolves correctly. The header cannot be spoofed by clients because
nginx overwrites it before forwarding.

### 3. Cert renewal systemd units (`deploy/ccswitch-cert-renew.{service,timer}`)

`tailscale cert` is idempotent — it only fetches a new cert when the existing one is near
expiry. A weekly oneshot service runs it, then reloads nginx:

```ini
# ccswitch-cert-renew.service
[Unit]
Description=Renew Tailscale TLS certificate for ccswitch
After=network-online.target tailscaled.service

[Service]
Type=oneshot
ExecStart=tailscale cert \
    --cert-file /etc/ccswitch/dobby.tail0dd570.ts.net.crt \
    --key-file  /etc/ccswitch/dobby.tail0dd570.ts.net.key \
    dobby.tail0dd570.ts.net
ExecStartPost=nginx -s reload
```

```ini
# ccswitch-cert-renew.timer
[Unit]
Description=Weekly Tailscale cert renewal for ccswitch

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

Let's Encrypt certs are 90-day; weekly checks provide ~12 renewal opportunities before
expiry.

---

## One-time migration on dobby

```bash
# 1. Provision cert (requires tailscale up + tailnet membership)
sudo tailscale cert \
    --cert-file /etc/ccswitch/dobby.tail0dd570.ts.net.crt \
    --key-file  /etc/ccswitch/dobby.tail0dd570.ts.net.key \
    dobby.tail0dd570.ts.net

# 2. Install nginx vhost
sudo cp deploy/ccswitch-tailnet.nginx /etc/nginx/sites-available/ccswitch-tailnet
sudo ln -sf /etc/nginx/sites-available/ccswitch-tailnet /etc/nginx/sites-enabled/
sudo nginx -t && sudo nginx -s reload

# 3. Install renewal timer
sudo cp deploy/ccswitch-cert-renew.service deploy/ccswitch-cert-renew.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccswitch-cert-renew.timer
```

---

## Files changed

| File | Change |
|------|--------|
| `deploy/ccswitch-tailnet.nginx` | New — nginx vhost config |
| `deploy/ccswitch-cert-renew.service` | New — cert renewal oneshot |
| `deploy/ccswitch-cert-renew.timer` | New — weekly renewal trigger |
| `server/auth/identity.py` | Fix — prefer `X-Real-IP` in `_whois_identity` |
| `specs/001-ccswitch-web/quickstart.md` | Update — replace `tailscale serve` with nginx instructions |

---

## What does NOT change

- Uvicorn bind address (`127.0.0.1:8787`) — unchanged.
- Tailscale allowlist enforcement — unchanged; the header-strip + `X-Real-IP` passthrough
  preserves identity resolution semantics.
- Cookie `Secure` flag — already set; works correctly once served over HTTPS.
- KinDash nginx vhost — separate `server_name`, untouched.
- The `ccswitch.service` / `ccswitch-daemon.service` systemd units — unchanged.
