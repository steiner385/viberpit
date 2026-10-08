"""No shipped unit may hard-depend on tailscaled.service.

2026-10-07: an apt full-upgrade on rocky upgraded the tailscale package. dpkg restarts a
package's service as a STOP followed by a START, and systemd propagates the stop half
of ``Requires=tailscaled.service`` to the dependent units but never the start. So
``viberpit-daemon.service`` drained and stopped, and ``viberpit-mounts.service`` (same
edge) unmounted the sshfs shares; with the ensure timer not armed, the session host
stayed down until someone noticed. Every routine tailscale update would repeat it.

Tailscale is a soft dependency: sshfs runs with ``reconnect`` and the daemon's RPC
clients retry, so a brief tailnet drop is survivable. ``After=`` keeps the boot
ordering; ``Wants=`` pulls tailscaled in without coupling stop/restart to it.

Hard edges BETWEEN viberpit units are a different matter and stay (the daemon still
``Requires=`` the mounts, #765 — see test_mounts_unit.py).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
HARD_DEPENDENCY = re.compile(r"^(Requires|BindsTo|PartOf|Requisite)=(.*)$", re.MULTILINE)
# Units whose work rides the tailnet and so must still start after tailscaled.
TAILNET_UNITS = ("viberpit-daemon-remote.service", "viberpit-mounts.service")


def _unit_files():
    return sorted(p for p in DEPLOY.rglob("*") if p.suffix in {".service", ".conf", ".timer"}
                  and p.is_file())


def test_no_unit_hard_depends_on_tailscaled():
    offenders = [
        f"{path.relative_to(ROOT)}: {match[0]}"
        for path in _unit_files()
        for match in HARD_DEPENDENCY.finditer(path.read_text())
        if "tailscaled.service" in match[2].split()
    ]
    assert not offenders, (
        "a dpkg restart of tailscaled would stop these units and never start them again "
        "— use Wants=tailscaled.service (keep After=):\n" + "\n".join(offenders)
    )


def test_tailnet_units_still_start_after_and_pull_in_tailscaled():
    for name in TAILNET_UNITS:
        unit = (DEPLOY / name).read_text()
        after = " ".join(re.findall(r"^After=(.*)$", unit, re.MULTILINE)).split()
        wants = " ".join(re.findall(r"^Wants=(.*)$", unit, re.MULTILINE)).split()
        assert "tailscaled.service" in after, f"{name} lost its boot ordering after tailscaled"
        assert "tailscaled.service" in wants, f"{name} no longer pulls tailscaled in at boot"
