from server.sessions import deploy_snapshot as ds


def test_write_then_read_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "live.json")   # parent dir does not exist yet
    ds.write_snapshot([{"id": "s1", "status": "working"}], path=p)
    assert ds.read_snapshot(path=p) == [{"id": "s1", "status": "working"}]


def test_read_missing_is_empty(tmp_path):
    assert ds.read_snapshot(path=str(tmp_path / "nope.json")) == []


def test_read_corrupt_is_empty(tmp_path):
    p = tmp_path / "live.json"; p.write_text("{not json")
    assert ds.read_snapshot(path=str(p)) == []


def test_clear_removes_file(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "idle"}], path=p)
    ds.clear_snapshot(path=p)
    assert ds.read_snapshot(path=p) == []
    ds.clear_snapshot(path=p)   # idempotent — no error
