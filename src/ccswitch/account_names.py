from __future__ import annotations
import re
from . import paths

_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


class InvalidAccountName(Exception):
    pass


def validate_account_name(name: str) -> bool:
    if not isinstance(name, str) or name in (".", ".."):
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
