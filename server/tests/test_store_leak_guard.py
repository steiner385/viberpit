"""The conftest real-store write guard (see server/tests/conftest.py).

Regression lock for the recurring phantom "alpha" account: a test that seeds a
credential via write_json_secure() outside its CCSWITCH_HOME patch resolves to
the user's real ~/.claude-accounts and leaks. The guard must turn that into a
loud failure, while leaving legitimate temp-dir writes untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ccswitch import paths
from ccswitch.creds import write_json_secure


def test_guard_blocks_write_to_real_store():
    """A write that resolves under the real ~/.claude-accounts — the exact
    mistake that leaked "alpha" — must raise, not silently write.

    conftest now redirects CCSWITCH_HOME to a temp dir by default, so this test
    points it back at the real store explicitly to exercise the backstop guard.
    The guard raises BEFORE any bytes touch disk, so no real write occurs."""
    real_home = str(Path.home() / ".claude-accounts")
    with patch.dict(os.environ, {"CCSWITCH_HOME": real_home}):
        target = paths.slot_path("phantom-guard-test")
        with pytest.raises(RuntimeError, match="REAL account store"):
            write_json_secure(target, {"accessToken": "x"})
        # And nothing was actually written to the real store.
        assert not target.exists()


def test_guard_allows_write_under_temp_store(tmp_path):
    """The correct pattern — seeding inside the CCSWITCH_HOME patch — is allowed."""
    store_home = tmp_path / "store"
    with patch.dict(os.environ, {"CCSWITCH_HOME": str(store_home)}):
        target = paths.slot_path("ok-acct")
        write_json_secure(target, {"accessToken": "ok"})
        assert target.exists()
        assert target.read_text()  # real bytes landed in the temp store
