"""Config snapshot / export / import / diff over the shared base.

SECURITY invariant under test (constitution II/IV): credential and identity
files are excluded from every capture AND from every write. A poisoned bundle
cannot smuggle a ``../evil`` path outside the base (tar-slip guard).
"""
import io
import tarfile
import time
from pathlib import Path

import pytest

from server.library import snapshots


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    # A representative config: settings + a skill + a command.
    (base / "settings.json").write_text('{"enabledPlugins": {}}')
    (base / "skills" / "foo").mkdir(parents=True)
    (base / "skills" / "foo" / "SKILL.md").write_text("---\nname: foo\n---\nbody\n")
    (base / "commands").mkdir()
    (base / "commands" / "bar.md").write_text("hi\n")
    return base


def test_is_excluded_predicate():
    excluded = [
        ".credentials.json",
        "x.credentials.json",
        ".claude.json",
        "plugins/cache/big.bin",
        "projects/foo.jsonl",
        "todos/x.json",
        "shell-snapshots/s",
        "statsig/s",
        "history/h",
    ]
    for rel in excluded:
        assert snapshots.is_excluded(rel), rel
    kept = [
        "settings.json",
        "skills/foo/SKILL.md",
        "plugins/installed_plugins.json",
        "commands/bar.md",
    ]
    for rel in kept:
        assert not snapshots.is_excluded(rel), rel


def test_create_and_list_snapshot(shared: Path):
    meta = snapshots.create_snapshot(label="first")
    assert meta["label"] == "first"
    assert meta["kind"] == "manual"
    assert meta["fileCount"] == 3  # settings + SKILL.md + bar.md
    assert meta["sizeBytes"] > 0
    assert meta["createdAt"]

    listed = snapshots.list_snapshots()
    assert len(listed) == 1
    assert listed[0]["id"] == meta["id"]


def test_snapshot_dir_missing_yields_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-such-base"))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    meta = snapshots.create_snapshot()
    assert meta["fileCount"] == 0


def test_credentials_excluded_from_snapshot_and_export(shared: Path):
    (shared / ".credentials.json").write_text('{"token": "SECRET"}')
    (shared / ".claude.json").write_text('{"identity": "SECRET"}')
    meta = snapshots.create_snapshot()

    members = snapshots._snapshot_members(meta["id"])
    assert ".credentials.json" not in members
    assert ".claude.json" not in members
    assert "settings.json" in members

    # Export bundle must not carry them either.
    bundle = snapshots.export_bundle()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        names = tar.getnames()
    assert ".credentials.json" not in names
    assert ".claude.json" not in names
    assert "settings.json" in names


def test_restore_snapshot_reverts_mutation(shared: Path):
    original = (shared / "settings.json").read_text()
    meta = snapshots.create_snapshot()

    (shared / "settings.json").write_text('{"enabledPlugins": {"changed": true}}')
    assert (shared / "settings.json").read_text() != original

    result = snapshots.restore_snapshot(meta["id"])
    assert (shared / "settings.json").read_text() == original
    assert result["restored"] == 3
    assert result["backupId"]

    kinds = {m["kind"] for m in snapshots.list_snapshots()}
    assert "pre-restore" in kinds


def test_restore_unknown_raises(shared: Path):
    with pytest.raises(FileNotFoundError):
        snapshots.restore_snapshot("deadbeef")


def test_diff_snapshot(shared: Path):
    meta = snapshots.create_snapshot()
    # add a new file, modify one, delete one.
    (shared / "commands" / "new.md").write_text("new\n")
    (shared / "settings.json").write_text('{"enabledPlugins": {"x": true}}')
    (shared / "commands" / "bar.md").unlink()

    diff = snapshots.diff_snapshot(meta["id"])
    assert diff["added"] == ["commands/new.md"]
    assert diff["modified"] == ["settings.json"]
    assert diff["removed"] == ["commands/bar.md"]


def test_diff_unknown_raises(shared: Path):
    with pytest.raises(FileNotFoundError):
        snapshots.diff_snapshot("deadbeef")


def test_export_import_preview_and_apply(shared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundle = snapshots.export_bundle()

    # Point the shared base at a fresh EMPTY dir: every member is a create.
    empty = tmp_path / "empty-shared"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))

    preview = snapshots.import_preview(bundle)
    assert sorted(preview["creates"]) == [
        "commands/bar.md",
        "settings.json",
        "skills/foo/SKILL.md",
    ]
    assert preview["overwrites"] == []
    assert preview["unchanged"] == []
    # Preview never writes.
    assert not (empty / "settings.json").exists()

    result = snapshots.import_apply(bundle)
    assert result["created"] == 3
    assert result["overwritten"] == 0
    assert result["backupId"]
    assert (empty / "settings.json").exists()
    assert (empty / "skills" / "foo" / "SKILL.md").exists()


def test_import_tarslip_member_not_written(shared: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Craft a malicious bundle with a traversal member.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = b"pwned"
        info = tarfile.TarInfo(name="../evil")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        good = b"ok"
        ginfo = tarfile.TarInfo(name="settings.json")
        ginfo.size = len(good)
        tar.addfile(ginfo, io.BytesIO(good))
    data = buf.getvalue()

    target = tmp_path / "target-shared"
    target.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))

    snapshots.import_apply(data)
    # The traversal member must NOT land outside the base.
    assert not (tmp_path / "evil").exists()
    assert not (target.parent / "evil").exists()


def test_snapshot_before_mutation_throttled(shared: Path):
    first = snapshots.snapshot_before_mutation()
    assert first is not None
    assert first["kind"] == "auto"
    # Immediate second call is throttled (< 5s since the last auto snapshot).
    second = snapshots.snapshot_before_mutation()
    assert second is None


def test_snapshot_before_mutation_not_throttled_when_stale(shared: Path):
    first = snapshots.snapshot_before_mutation()
    assert first is not None
    # Backdate the auto snapshot's tar mtime past the throttle window.
    tar = snapshots._tar_path(first["id"])
    old = time.time() - 3600
    import os

    os.utime(tar, (old, old))
    second = snapshots.snapshot_before_mutation()
    assert second is not None
    assert second["id"] != first["id"]
