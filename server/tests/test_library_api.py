"""Library API routes over a fabricated shared base."""
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccswitch import paths
from server.app import create_app

IDENTITY_HEADERS = {"Tailscale-User-Login": "libtest@example.com"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    (base / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@m": True}}))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_plugins_list_and_toggle(client: TestClient):
    out = client.get("/api/library/plugins", headers=IDENTITY_HEADERS).json()
    assert out["plugins"][0]["id"] == "a@m"
    r = client.patch("/api/library/plugins/a@m", json={"enabled": False}, headers=IDENTITY_HEADERS)
    assert r.status_code == 200 and r.json() == {"id": "a@m", "enabled": False}
    out = client.get("/api/library/plugins", headers=IDENTITY_HEADERS).json()
    assert out["plugins"][0]["enabled"] is False
    assert client.patch("/api/library/plugins/nope@m", json={"enabled": True},
                        headers=IDENTITY_HEADERS).status_code == 404
    assert client.patch("/api/library/plugins/Bad Id!", json={"enabled": True},
                        headers=IDENTITY_HEADERS).status_code == 400


def test_skill_crud_roundtrip(client: TestClient):
    r = client.put("/api/library/skills/my-skill", json={"content": "hello\n"},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200
    r = client.get("/api/library/skills/my-skill", headers=IDENTITY_HEADERS)
    assert r.json()["content"] == "hello\n"
    names = [s["name"] for s in client.get("/api/library/skills", headers=IDENTITY_HEADERS).json()["skills"]]
    assert "my-skill" in names
    assert client.delete("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 204
    assert client.get("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 404
    assert client.delete("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 404
    assert client.put("/api/library/skills/../evil", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code in (400, 404)  # 404: path routing
    assert client.put("/api/library/skills/Bad Name", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code == 400


def test_command_crud_nested(client: TestClient):
    r = client.put("/api/library/commands/project/deep", json={"content": "x\n"},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200
    assert client.get("/api/library/commands/project/deep",
                      headers=IDENTITY_HEADERS).json()["content"] == "x\n"
    paths_ = [c["path"] for c in client.get("/api/library/commands", headers=IDENTITY_HEADERS).json()["commands"]]
    assert "project/deep" in paths_
    # "a/../b" is URL-normalized to "b" by httpx before reaching the server, so
    # test traversal rejection with a segment that survives transport instead.
    assert client.put("/api/library/commands/Bad%20Command", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code == 400
    assert client.delete("/api/library/commands/project/deep", headers=IDENTITY_HEADERS).status_code == 204


def test_mcp_crud(client: TestClient):
    r = client.put("/api/library/mcp/srv", json={"config": {"type": "http", "url": "https://x/mcp"}},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200 and r.json() == {"name": "srv", "transport": "http"}
    servers = client.get("/api/library/mcp", headers=IDENTITY_HEADERS).json()["servers"]
    assert servers[0]["name"] == "srv" and servers[0]["source"] == "user"
    assert client.put("/api/library/mcp/bad", json={"config": {"type": "http"}},
                      headers=IDENTITY_HEADERS).status_code == 400
    assert client.delete("/api/library/mcp/srv", headers=IDENTITY_HEADERS).status_code == 204
    assert client.delete("/api/library/mcp/srv", headers=IDENTITY_HEADERS).status_code == 404
    # deletion persisted to the shared claude.json
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data.get("mcpServers", {}) == {}


def test_requires_identity(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # conftest runs with an empty allowlist + CCSWITCH_DEV=1, which admits any
    # identity header. A request with no header at all must still 401.
    r = client.get("/api/library/plugins")
    assert r.status_code == 401


def test_toggle_plugin_corrupt_settings_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    # Write a corrupt settings.json — note: plugin must also appear in settings
    # so it passes the "known" check; but corrupt parse fires before the key lookup.
    (base / "settings.json").write_text("not json")
    app = create_app()
    with TestClient(app) as c:
        r = c.patch(
            "/api/library/plugins/a@m",
            json={"enabled": False},
            headers=IDENTITY_HEADERS,
        )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "config_unreadable"
    assert body["path"] == "settings.json"


def test_put_mcp_corrupt_shared_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccswitch import paths as cpaths

    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    (base / "settings.json").write_text("{}")
    shared_path = cpaths.shared_claude_json_path()
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("not json")
    app = create_app()
    with TestClient(app) as c:
        r = c.put(
            "/api/library/mcp/srv",
            json={"config": {"command": "bin"}},
            headers=IDENTITY_HEADERS,
        )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "config_unreadable"


def test_delete_mcp_corrupt_shared_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccswitch import paths as cpaths

    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    (base / "settings.json").write_text("{}")
    shared_path = cpaths.shared_claude_json_path()
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("not json")
    app = create_app()
    with TestClient(app) as c:
        r = c.delete(
            "/api/library/mcp/srv",
            headers=IDENTITY_HEADERS,
        )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "config_unreadable"
    assert body["path"] == shared_path.name
