"""Environment-prefix compatibility: ``CCSWITCH_*`` <-> ``VIBERPIT_*``.

Phase 0b of the ViberPit rebrand. This is the shim that decouples *renaming the
reads* from *migrating the env files*: once every entrypoint mirrors, the ~211
distinct ``CCSWITCH_<X>`` names scattered across the tree can be renamed by
find-and-replace while ``/etc/ccswitch/*.env`` on every host keeps working
untouched — and a host that has already migrated its env file keeps feeding code
that still reads the legacy name.

Two properties make it safe:

* **Fill, never overwrite.** A name that the process was actually launched with
  always wins over the mirror of its counterpart. If both are set and disagree,
  both keep their launched values — the shim never silently rewrites an operator's
  explicit choice.
* **Launch-time snapshot, not a live sync.** :func:`mirror_legacy_env` mirrors the
  real environment exactly once per process (see ``force``). A later
  ``del os.environ["CCSWITCH_X"]`` therefore stays deleted, rather than being
  resurrected from the mirror copy by the next call.

The sweep is over ``os.environ`` rather than a list of known names, so the
dynamically-built ``CCSWITCH_OAT_<ACCOUNT>`` names (``crypto.py``) are covered
without enumerating accounts.
"""
from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

from ccswitch import brand

logger = logging.getLogger(__name__)

#: The two prefixes, in the order they are read from. Both directions run.
NEW_PREFIX = brand.ENV_PREFIX
LEGACY_PREFIX = brand.LEGACY_ENV_PREFIX

_mirrored = False


def _fill(environ: MutableMapping[str, str], source: str, target: str) -> dict[str, str]:
    """Copy every ``<source><X>`` to ``<target><X>`` that is not already set."""
    filled: dict[str, str] = {}
    # Snapshot the keys: we mutate `environ` inside the loop.
    for name in [k for k in environ if k.startswith(source)]:
        suffix = name[len(source):]
        if not suffix:  # a bare "CCSWITCH_" is not a setting; nothing to mirror
            continue
        counterpart = target + suffix
        if counterpart in environ:
            continue
        environ[counterpart] = environ[name]
        filled[counterpart] = name
    return filled


def mirror_legacy_env(
    environ: MutableMapping[str, str] | None = None, *, force: bool = False
) -> dict[str, str]:
    """Mirror ``CCSWITCH_*`` to ``VIBERPIT_*`` and back, filling only unset names.

    Call this as the first statement of every entrypoint, before any setting is
    read — ``server/app.py`` builds its app at import time, so for the server the
    call lives in ``server/__init__.py``, which Python runs before any
    ``server.*`` module body.

    Args:
        environ: mapping to mirror in place. Defaults to ``os.environ``, in which
            case the mirror runs only once per process.
        force: re-run against ``os.environ`` even if it has already been mirrored.

    Returns:
        ``{name_that_was_filled: name_it_was_copied_from}`` — empty when there was
        nothing to do (including the already-mirrored no-op). Callers may use it
        for provenance ("which prefix supplied this?"); it holds names, no values.
    """
    global _mirrored
    if environ is None:
        if _mirrored and not force:
            return {}
        environ = os.environ
        _mirrored = True

    filled = _fill(environ, LEGACY_PREFIX, NEW_PREFIX)
    filled.update(_fill(environ, NEW_PREFIX, LEGACY_PREFIX))
    if filled:
        # Names only — these variables carry master keys and DB URLs.
        logger.debug(
            "env prefix mirror filled %d name(s): %s",
            len(filled),
            ", ".join(f"{new}<-{old}" for new, old in sorted(filled.items())),
        )
    return filled
