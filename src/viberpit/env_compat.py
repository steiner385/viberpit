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
import re
from collections.abc import Mapping, MutableMapping

from viberpit import brand

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


#: Names (the part after either prefix) :func:`withheld_env` keeps: what a session's own
#: tooling reads, none of it secret. Each fails on ``""`` rather than falling back, and
#: an allowlisted name is still withheld when its value is secret-shaped
#: (:func:`is_secret_shaped`).
WITHHELD_ENV_KEEP: frozenset[str] = frozenset({
    # ``fleet_role.daemon_role()`` raises on "" — so every ``viberpit``/``ccrun`` command
    # and the core tests in a session on a worker (rocky). "worker" also makes that
    # tooling stricter, never looser.
    "DAEMON_ROLE",
    # The ``claude`` command ``ccrun``/``ccmax``/reauth launch; ``shlex.split("")`` is an
    # empty argv.
    "CLAUDE_BIN",
    # ``paths.root()``, the account store every ``viberpit`` command reads: ``Path("")``
    # is the cwd, and the default is the wrong store on a host that sets it.
    "HOME",
    # ``viberpit``'s clone root (``cli._repo_root``); ``""`` is not a directory.
    "REPO_ROOT",
})

#: Name fragments that mark a variable as carrying a secret (or a path to one).
_SECRET_NAME_MARKERS: tuple[str, ...] = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "DB_URL", "DSN", "CRED",
    "_OAT_",  # <prefix>OAT_<ACCOUNT>: an account's setup-token (crypto.py)
)

#: A URL with userinfo (``scheme://user[:password]@host``).
_URL_USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+@")


def is_secret_shaped(name: str, value: str) -> bool:
    """Does *name* name a secret (``KEY``, ``TOKEN``, ``DB_URL``, ``CRED``, an account's
    ``OAT_``, …), or *value* carry URL userinfo? Never kept by :func:`withheld_env`;
    refused when set but empty by ``server.startup_guard``."""
    upper = name.upper()
    return any(m in upper for m in _SECRET_NAME_MARKERS) or bool(_URL_USERINFO.match(value))


def withheld_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``{name: ""}`` for every key of this namespace in *environ*, in both prefixes,
    except a :data:`WITHHELD_ENV_KEEP` name whose value is not secret-shaped: what every
    agent CLI's env overrides (``claude_sdk_factory.real_sdk_factory``,
    ``account_auth.helper_env``). An agent's shell inherits its CLI's env, and the
    namespace carries the daemon's secrets — the master key, the DB URL with its
    password, the GitHub project token (#3386; first seen in a sandboxed shell, #3367).

    Emptied, not dropped: the SDK builds the CLI's env as ``{**os.environ, ...,
    **options.env}`` (claude-agent-sdk 0.2.105 ``SubprocessCLITransport.connect``), so a
    key left out of ``options.env`` is inherited anyway; only an override removes the
    value. Settings read an empty value as unset (``server.config.Settings``,
    ``env_ignore_empty``), so this repo's own tests still run in a session's shell.
    Nothing the CLI itself runs needs the namespace: session tools and SDK hooks run in
    the daemon, and the Codex app-server child never gets it (``CHILD_ENV_ALLOWLIST``);
    the kept names are for the ``viberpit`` tooling an agent runs in its shell.

    Defence in depth, not containment: a process running as the daemon's user can still
    read that user's own files (the account store, ``~/.config/gh``). Real containment
    is the sandbox (D-23)."""
    out: dict[str, str] = {}
    for name, value in environ.items():
        for prefix in (NEW_PREFIX, LEGACY_PREFIX):
            if name.startswith(prefix):
                if not (name[len(prefix):] in WITHHELD_ENV_KEEP
                        and not is_secret_shaped(name, value)):
                    out[name] = ""
                break
    return out
