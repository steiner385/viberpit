import os
import ssl
from unittest import mock

import pytest

from server.ipc.tls import (
    ClientTLSFiles, TLSConfigError, client_ssl_context, client_tls_files, daemon_tls_files,
)


def test_daemon_tls_files_missing_raises():
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(TLSConfigError):
            daemon_tls_files()


def test_client_tls_files_unreadable_raises(tmp_path):
    env = {
        "CCSWITCH_SESSIOND_CLIENT_CERT": str(tmp_path / "nope.pem"),
        "CCSWITCH_SESSIOND_CLIENT_KEY": str(tmp_path / "nope.key"),
        "CCSWITCH_SESSIOND_CLIENT_CA": str(tmp_path / "nope.ca"),
    }
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(TLSConfigError):
            client_tls_files()


def test_daemon_tls_files_reads_env(mtls_certs):
    env = {
        "CCSWITCH_SESSIOND_TLS_CERT": mtls_certs.server_cert,
        "CCSWITCH_SESSIOND_TLS_KEY": mtls_certs.server_key,
        "CCSWITCH_SESSIOND_TLS_CA": mtls_certs.ca,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        files = daemon_tls_files()
    assert files.cert == mtls_certs.server_cert and files.ca == mtls_certs.ca


def test_client_ssl_context_loads(mtls_certs):
    ctx = client_ssl_context(ClientTLSFiles(
        cert=mtls_certs.client_cert, key=mtls_certs.client_key, ca=mtls_certs.ca))
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # the client verifies the server
