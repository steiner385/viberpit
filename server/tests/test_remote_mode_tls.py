import os
from unittest import mock

from server.ipc.paths import Endpoint


def test_daemon_present_tcp_false_when_unreachable():
    from server.app import _daemon_present
    # Nothing listening on this ephemeral port.
    assert _daemon_present(Endpoint("https", host="127.0.0.1", port=9)) is False


def test_create_app_remote_mode_with_https_endpoint(mtls_certs, monkeypatch):
    """An explicit https endpoint → remote mode + DaemonClient pointed at it."""
    env = {
        "CCSWITCH_SESSIOND_ENDPOINT": "https://127.0.0.1:9443",
        "CCSWITCH_SESSIOND_CLIENT_CERT": mtls_certs.client_cert,
        "CCSWITCH_SESSIOND_CLIENT_KEY": mtls_certs.client_key,
        "CCSWITCH_SESSIOND_CLIENT_CA": mtls_certs.ca,
        "CCSWITCH_DEV": "1",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        from server.app import create_app
        app = create_app()
        assert app.state.remote_mode is True
        assert app.state.daemon_endpoint == Endpoint("https", host="127.0.0.1", port=9443)
        assert app.state.daemon_client_ssl is not None
