"""McpViewStore: the runner-side cache of the MCP store's public view (#3287)."""
import os

from viberpit.mcp_view_store import McpViewStore


def test_sync_persists_and_reloads(tmp_path):
    store = McpViewStore(tmp_path)
    assert store.bundles() is None
    assert store.sync({"k1": {"serverName": "aws", "accessToken": "at", "expiresAt": 5}}, ["old"]) is True
    assert McpViewStore(tmp_path).bundles() == {"k1": {"serverName": "aws", "accessToken": "at", "expiresAt": 5}}
    assert McpViewStore(tmp_path).drop_keys() == frozenset({"old"})
    assert oct(os.stat(tmp_path / "mcp-view.json").st_mode & 0o777) == "0o600"


def test_sync_strips_refresh_token(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": {"accessToken": "at", "refreshToken": "rt"}})
    assert store.bundles() == {"k1": {"accessToken": "at"}}
    assert "rt" not in (tmp_path / "mcp-view.json").read_text()


def test_sync_writes_only_on_change(tmp_path):
    store = McpViewStore(tmp_path)
    assert store.sync({"k1": {"accessToken": "a"}}) is True
    assert store.sync({"k1": {"accessToken": "a"}}) is False


def test_non_dict_bundles_are_ignored(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": "junk", "k2": {"accessToken": "a"}})
    assert store.bundles() == {"k2": {"accessToken": "a"}}


def test_bundles_returns_a_copy(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": {"accessToken": "a"}})
    store.bundles()["k1"]["accessToken"] = "x"
    assert store.bundles()["k1"]["accessToken"] == "a"


def test_torn_cache_file_loads_as_never_synced(tmp_path):
    (tmp_path / "mcp-view.json").write_text("{not json")
    store = McpViewStore(tmp_path)
    assert store.bundles() is None
    assert store.drop_keys() == frozenset()
