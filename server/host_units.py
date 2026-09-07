"""Installed service identity, resolved on the host where the command will run."""
from __future__ import annotations

import subprocess
from typing import Callable

from ccswitch import brand

Run = Callable[..., subprocess.CompletedProcess]
_SUFFIXES = {'edge': '', 'daemon': '-daemon', 'controller': '-deploy-controller'}


def resolve_unit(role: str, *, run: Run = subprocess.run,
                 prefix: list[str] | None = None) -> str:
    """Prefer the new name, following aliases to their canonical systemd Id.

    ``prefix`` is the validated SSH argv for remote callers. Never use local unit
    state to choose a remote unit. Missing hosts/units default to the new name;
    the subsequent systemctl operation reports the actual failure.
    """
    suffix = _SUFFIXES[role]
    candidates = [f'{slug}{suffix}.service' for slug in (brand.SLUG, brand.LEGACY_SLUG)]
    for unit in candidates:
        try:
            result = run([*(prefix or []), 'systemctl', 'show', '-p', 'LoadState',
                          '-p', 'Id', unit], capture_output=True, text=True, timeout=15)
            properties = dict(line.split('=', 1) for line in (result.stdout or '').splitlines()
                              if '=' in line)
            if result.returncode == 0 and properties.get('LoadState') == 'loaded':
                identity = properties.get('Id')
                return identity if identity in candidates else unit
        except (OSError, subprocess.SubprocessError):
            continue
    return candidates[0]
