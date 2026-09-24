"""SQLite files are refused on a shared mount at open time (#2016)."""
from __future__ import annotations

import pytest

from server import localstate
from server.localstate import SharedSqliteRefused, assert_local_sqlite, sqlite_file_from_url

# The deployed fleet's shape: the account store is sshfs, the rest of $HOME is local.
_FLEET = (("/", "ext4"), ("/home/u/.claude-accounts", "fuse.sshfs"))


def test_sqlite_on_sshfs_is_refused():
    with pytest.raises(SharedSqliteRefused, match="fuse.sshfs"):
        assert_local_sqlite("/home/u/.claude-accounts/app.db", mounts=_FLEET)


def test_sqlite_on_local_disk_is_allowed():
    assert_local_sqlite("/home/u/.local/state/viberpit/app.db", mounts=_FLEET)


def test_undeterminable_mount_table_is_allowed():
    # A refused app DB is a refused process; an unreadable mount table is not the
    # fleet's shape, so it does not block startup.
    assert_local_sqlite("/home/u/.claude-accounts/app.db", mounts=())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:////home/u/.claude-accounts/app.db", "/home/u/.claude-accounts/app.db"),
        ("sqlite:///relative.db", "relative.db"),
        ("sqlite+aiosqlite:///:memory:", None),
        ("sqlite+aiosqlite://", None),
        ("postgresql+asyncpg://u@h/db", None),
    ],
)
def test_sqlite_file_from_url(url, expected):
    assert sqlite_file_from_url(url) == expected


def _pretend_shared(monkeypatch, tmp_path):
    """Make *tmp_path* read as an sshfs mount for every open-time check."""
    fleet = (("/", "ext4"), (str(tmp_path.resolve()), "fuse.sshfs"))
    real = localstate.filesystem_type_for
    monkeypatch.setattr(
        localstate, "filesystem_type_for", lambda path, mounts=None: real(path, mounts=fleet)
    )


def test_app_engine_refuses_a_shared_sqlite_file(monkeypatch, tmp_path):
    from server.db import _make_engine

    _pretend_shared(monkeypatch, tmp_path)
    with pytest.raises(SharedSqliteRefused):
        _make_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")


def test_history_store_refuses_a_shared_file(monkeypatch, tmp_path):
    from server.pipeline.history import HistoryStore

    _pretend_shared(monkeypatch, tmp_path)
    with pytest.raises(SharedSqliteRefused):
        HistoryStore(str(tmp_path / "pipeline_history.db"))


def test_history_migrate_refuses_before_writing(monkeypatch, tmp_path):
    from server.pipeline.history_migrate import migrate_history

    _pretend_shared(monkeypatch, tmp_path)
    dest = tmp_path / "pipeline_history.db"
    with pytest.raises(SharedSqliteRefused):
        migrate_history(source_path=str(tmp_path / "missing.db"), dest_path=str(dest))
    assert not dest.exists()
