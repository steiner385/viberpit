"""ledger.json must be written atomically and survive a corrupt read.

The live ledger was found holding TWO concatenated JSON documents on 2026-08-22:

    { "accounts": {} }{ "accounts": { ...4 real accounts... } }

`locked_ledger` opened the file with mode "a+", which sets O_APPEND — and under
O_APPEND every write goes to EOF regardless of seek(). So a second writer that
read the file mid-truncate (seeing it empty) appended its `{"accounts": {}}`
after the good document. flock was supposed to prevent that, but the account
store is an sshfs mount on the remote host and flock does NOT propagate to the
server: rocky acquired the lock while dobby held it (verified 2026-08-22).

Every ledger operation then raised JSONDecodeError, which breaks account
selection, usage tracking, reservations and enable/disable on BOTH hosts.
"""
from __future__ import annotations

import json
import os

import pytest

from viberpit import ledger, paths


@pytest.fixture()
def led_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path))
    return tmp_path


def test_roundtrip(led_home):
    with ledger.locked_ledger() as led:
        led["accounts"]["a"] = {"enabled": False}
    with ledger.locked_ledger() as led:
        assert led["accounts"]["a"] == {"enabled": False}


def test_write_does_not_append(led_home):
    """The file must contain exactly ONE json document after repeated writes.

    With O_APPEND this test fails: each write lands at EOF, so the file grows
    into a concatenation instead of being replaced.
    """
    for i in range(5):
        with ledger.locked_ledger() as led:
            led["accounts"]["a"] = {"n": i}

    raw = paths.ledger_path().read_text(encoding="utf-8")
    # A single document parses cleanly; a concatenation raises "Extra data".
    doc = json.loads(raw)
    assert doc["accounts"]["a"] == {"n": 4}


def test_recovers_from_a_concatenated_file(led_home, caplog):
    """A file already corrupted this way must not brick every account operation.

    Recover the richest document rather than raising: crashing here takes down
    account selection entirely, which is worse than continuing from the most
    complete state we can prove is intact.
    """
    p = paths.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    good = {"accounts": {"real": {"enabled": True}, "other": {"enabled": False}}}
    p.write_text(json.dumps({"accounts": {}}) + json.dumps(good), encoding="utf-8")

    with ledger.locked_ledger() as led:
        assert set(led["accounts"]) == {"real", "other"}, "must keep the richer doc"

    # And the file is left repaired — a single parseable document.
    assert json.loads(p.read_text(encoding="utf-8"))["accounts"]["other"] == {"enabled": False}


def test_truly_unparseable_file_still_raises(led_home):
    """Garbage that is not a concatenation of documents must NOT be silently
    replaced with an empty ledger — that would erase every account's state."""
    p = paths.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("this is not json at all", encoding="utf-8")

    with pytest.raises(Exception):
        with ledger.locked_ledger():
            pass


def test_file_mode_is_owner_only(led_home):
    with ledger.locked_ledger() as led:
        led["accounts"]["a"] = {}
    assert oct(os.stat(paths.ledger_path()).st_mode)[-3:] == "600"


# --- transient mount errors on lock acquisition (#2291) -----------------------------
#
# rocky's account store is an sshfs mount; a connection blip surfaces as EPERM from
# `open()` on the healthy lock file. One skipped sweep per blip was filing four
# separate auto-error issues, so lock acquisition retries briefly before giving up.


def _fail_lock_opens(monkeypatch, n: int, err: int):
    """Make the first *n* opens of the lock file raise ``OSError(err)``."""
    real_open = open
    calls = {"n": 0}

    def flaky(file, *a, **kw):
        if str(file).endswith(".lock"):
            calls["n"] += 1
            if calls["n"] <= n:
                raise OSError(err, os.strerror(err), str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    monkeypatch.setattr(ledger.time, "sleep", lambda _s: None)
    return calls


def test_lock_open_retries_a_transient_eperm(led_home, monkeypatch):
    import errno
    calls = _fail_lock_opens(monkeypatch, 2, errno.EPERM)
    with ledger.locked_ledger() as led:
        led["accounts"]["a"] = {"ok": True}
    assert calls["n"] == 3
    assert ledger.read_ledger()["accounts"]["a"] == {"ok": True}


def test_lock_open_reraises_once_the_budget_is_spent(led_home, monkeypatch):
    import errno
    calls = _fail_lock_opens(monkeypatch, 99, errno.EPERM)
    with pytest.raises(PermissionError), ledger.locked_ledger():
        pass
    assert calls["n"] == len(paths.TRANSIENT_RETRY_DELAYS_S) + 1


def test_lock_open_does_not_retry_a_real_permission_error(led_home, monkeypatch):
    import errno
    calls = _fail_lock_opens(monkeypatch, 99, errno.EACCES)
    with pytest.raises(PermissionError), ledger.locked_ledger():
        pass
    assert calls["n"] == 1
