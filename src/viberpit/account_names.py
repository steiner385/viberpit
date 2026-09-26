from __future__ import annotations
import re
from . import paths

_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


class InvalidAccountName(Exception):
    pass


# Names for which `store_dir() / name` does NOT yield a private per-account path.
_ROOT_ALIASES = frozenset({"", ".", ".."})


def aliases_store_root(name: object) -> bool:
    """True when *name* would collapse a per-account path onto the store ROOT.

    ``Path(store) / ""`` is ``Path(store)`` itself, and ``"."`` / ``".."`` walk to it
    or out of it — so a name in this set turns every per-account helper
    (``slot_path``, ``account_cfg_dir``, ``copilot_token_dir``) into a handle on
    SHARED state rather than one account's own. Because the shared root always
    exists, existence probes over such a path answer "yes" for an account that does
    not exist. That is how a stray ``.cred.json`` became a phantom account named
    ``""`` that ``provider_of`` then reported as Copilot, wedging live sessions.

    This is the single definition of that hazard: enumeration filters on it,
    ``provider_of`` short-circuits on it, and ``validate_account_name`` rejects it.
    """
    return not isinstance(name, str) or name.strip() in _ROOT_ALIASES


def validate_account_name(name: str) -> bool:
    if aliases_store_root(name):
        return False
    if not _RE.match(name):
        return False
    try:
        slot = paths.slot_path(name).resolve()
        return slot.is_relative_to(paths.store_dir().resolve())
    except (OSError, ValueError):
        return False


def assert_valid_account_name(name: str) -> None:
    if not validate_account_name(name):
        raise InvalidAccountName(name)
