"""Integration test: /ws/vnc is mounted and returns close code 4401 when x11 is disabled."""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

_IDENTITY_HEADERS = {"Tailscale-User-Login": "vnctest@example.com"}


@pytest.fixture()
def app_client(tmp_path):
    """Build a TestClient for the app with x11 disabled (None)."""
    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
        "CCSWITCH_DEV": "1",
    })

    with env_patch:
        app = create_app()
        # Force x11 to None (disabled path)
        app.state.x11 = None
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_vnc_ws_disabled_closes_4401(app_client):
    """When x11 is None, /ws/vnc accepts then immediately closes with code 4401."""
    from starlette.websockets import WebSocketDisconnect

    with app_client.websocket_connect("/ws/vnc?session=S", headers=_IDENTITY_HEADERS) as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_bytes()
        assert ei.value.code == 4401, (
            f"expected close code 4401 (x11 disabled), got {ei.value.code}"
        )
