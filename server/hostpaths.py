"""Well-known host paths — the ONE place the /etc, /run, /var/lib, /var/log stems are
spelled. They mirror the systemd units in deploy/ (RuntimeDirectory=, StateDirectory=,
EnvironmentFile=) and flip with them in rebrand Phase 3 via ``brand.HOST_SLUG``.
Env overrides (``CCSWITCH_DEPLOY_SOCKET`` …) stay at the call sites; these are defaults."""
from __future__ import annotations

from ccswitch import brand

RUN_DIR = f"/run/{brand.HOST_SLUG}"
SESSIOND_SOCKET = f"{RUN_DIR}/sessiond.sock"
DEPLOY_SOCKET = f"{RUN_DIR}/deploy.sock"
# Same directory as the socket: the unit's RuntimeDirectory=ccswitch makes it writable
# and RuntimeDirectoryPreserve=yes keeps it across the controller's own restart — the
# exact window the persisted status must survive (#865). Wiped on reboot, which is
# fine: after a reboot "no deploy in progress" is the truth.
DEPLOY_STATUS_PATH = f"{RUN_DIR}/deploy-status.json"

STATE_DIR = f"/var/lib/{brand.HOST_SLUG}"
HEALTH_STATE_DIR = f"{STATE_DIR}/health"
DEPLOY_SNAPSHOT_PATH = f"{STATE_DIR}/deploy-snapshot/live.json"

LOG_DIR = f"/var/log/{brand.HOST_SLUG}"

ETC_DIR = f"/etc/{brand.HOST_SLUG}"
ENV_FILE = f"{ETC_DIR}/{brand.HOST_SLUG}.env"

# Deliberately NOT owned here — each has its own reason to stay off brand.HOST_SLUG:
# - /var/lib/ccswitch-codex-auth (server/codex/auth_endpoint.py, spec §1: until
#   credential rotation)
# - <state>/ccswitch/codex (server/codex/manager.py)
# - scripts/ccswitch-health-check.sh (bash; mirrors daemon_health.py)
