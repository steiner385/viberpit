"""Compat shim: ``ccswitch`` is now ``viberpit`` (Phase 5a of the rebrand, #1374).

This package ships no code of its own. Importing it installs a :mod:`sys.meta_path`
finder that redirects any ``ccswitch`` / ``ccswitch.<sub>`` import to the identical
``viberpit`` / ``viberpit.<sub>`` module object — not a copy, so
``sys.modules["ccswitch.brand"] is sys.modules["viberpit.brand"]`` and a patch
applied under either name is visible under both (``unittest.mock.patch`` and
``__import__`` resolve submodules through the same import machinery, so this
covers ``import ccswitch.foo``, ``from ccswitch import foo``, and
``patch("ccswitch.foo.bar")`` alike).

Kept for one release so external scripts, cron jobs, and stale ``PYTHONPATH``
entries pinned to the pre-rebrand package name keep working. Remove this package
once that window closes (track removal in a follow-up issue).
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from types import ModuleType

_OLD = __name__  # "ccswitch"
_NEW = "viberpit"
_OLD_PREFIX = _OLD + "."


class _ViberpitAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolves ``ccswitch.<sub>`` to the already-imported ``viberpit.<sub>`` module."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _OLD and not fullname.startswith(_OLD_PREFIX):
            return None
        if fullname == _OLD:
            return None  # this package's own __init__.py is loaded normally
        real_name = _NEW + fullname[len(_OLD):]
        try:
            importlib.import_module(real_name)
        except ImportError:
            return None
        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec) -> ModuleType:
        real_name = _NEW + spec.name[len(_OLD):]
        module = sys.modules[real_name]
        # `importlib.util.module_from_spec` is about to overwrite this SHARED
        # module object's `__spec__` (unconditionally) to describe the alias
        # name instead of its real one, while leaving `__package__` (already
        # set from its real import) untouched — the mismatch trips a
        # DeprecationWarning on the module's own next `from . import sibling`.
        # Stash the real spec so `exec_module` can put it back.
        spec.loader_state = getattr(module, "__spec__", None)
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Nothing left to execute — the real module already ran. Restore its
        # own `__spec__` so it keeps resolving relative imports against its
        # real package rather than this alias.
        real_spec = getattr(module.__spec__, "loader_state", None)
        if real_spec is not None:
            module.__spec__ = real_spec


sys.meta_path.insert(0, _ViberpitAliasFinder())
