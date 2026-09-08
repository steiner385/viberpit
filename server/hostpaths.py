"""Well-known host paths — the ONE place the /etc, /run, /var/lib, /var/log stems are
spelled. They mirror the systemd units in deploy/ (RuntimeDirectory=, StateDirectory=,
EnvironmentFile=), preferring canonical roots while accepting legacy installations.
Env overrides (``CCSWITCH_DEPLOY_SOCKET`` …) stay at the call sites; these are defaults."""
from __future__ import annotations

from pathlib import Path

from ccswitch import brand


def prefer_existing(canonical: str, legacy: str) -> str:
    """Use canonical paths first, preserving existing legacy installations."""
    return canonical if Path(canonical).exists() or not Path(legacy).exists() else legacy


RUN_DIR = prefer_existing(f"/run/{brand.HOST_SLUG}", f"/run/{brand.LEGACY_SLUG}")
SESSIOND_SOCKET = f"{RUN_DIR}/sessiond.sock"
DEPLOY_SOCKET = f"{RUN_DIR}/deploy.sock"
# Same directory as the socket: the unit's RuntimeDirectory=viberpit makes it writable
# and RuntimeDirectoryPreserve=yes keeps it across the controller's own restart — the
# exact window the persisted status must survive (#865). Wiped on reboot, which is
# fine: after a reboot "no deploy in progress" is the truth.
DEPLOY_STATUS_PATH = f"{RUN_DIR}/deploy-status.json"

STATE_DIR = prefer_existing(f"/var/lib/{brand.HOST_SLUG}", f"/var/lib/{brand.LEGACY_SLUG}")
HEALTH_STATE_DIR = f"{STATE_DIR}/health"
DEPLOY_SNAPSHOT_PATH = f"{STATE_DIR}/deploy-snapshot/live.json"

LOG_DIR = prefer_existing(f"/var/log/{brand.HOST_SLUG}", f"/var/log/{brand.LEGACY_SLUG}")

ETC_DIR = prefer_existing(f"/etc/{brand.HOST_SLUG}", f"/etc/{brand.LEGACY_SLUG}")
ENV_FILE = prefer_existing(f"{ETC_DIR}/{brand.HOST_SLUG}.env",
                           f"{ETC_DIR}/{brand.LEGACY_SLUG}.env")

# Deliberately NOT owned here — each has its own reason to stay off brand.HOST_SLUG:
# - /var/lib/ccswitch-codex-auth (server/codex/auth_endpoint.py, spec §1: until
#   credential rotation)
# - <state>/ccswitch/codex (server/codex/manager.py)
# - scripts/ccswitch-health-check.sh (bash; mirrors daemon_health.py)
