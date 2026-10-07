"""Daemon mTLS certs must survive OpenSSL's strict X.509 checks.

Python 3.13+ turns on ``ssl.VERIFY_X509_STRICT`` in ``create_default_context()``. Under
it a CA with no ``keyUsage`` extension is rejected outright ("CA cert does not include
key usage extension"). Both fleet CAs ever minted by ``scripts/viberpit-mint-mtls.sh``
lacked one, so the 2026-10-07 Ubuntu 26.04 upgrade (Python 3.12 → 3.14) cut the edge
off from every daemon at once and the fleet had to be re-keyed onto a fresh CA.

CI runs Python 3.12, whose default context is NOT strict, so these tests set the flag
explicitly — otherwise they would pass on CI and the regression would only surface on
the next host upgrade.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from server.ipc.tls import ClientTLSFiles, client_ssl_context

ROOT = Path(__file__).resolve().parents[2]
MINT_SCRIPT = ROOT / "scripts" / "viberpit-mint-mtls.sh"


def _strict_mtls_handshake(*, ca: str, server_cert: str, server_key: str,
                           client_cert: str, client_key: str, hostname: str) -> None:
    """Run a full mutual-TLS handshake in memory with strict X.509 on both sides."""
    client_ctx = client_ssl_context(ClientTLSFiles(cert=client_cert, key=client_key, ca=ca))
    client_ctx.verify_flags |= ssl.VERIFY_X509_STRICT

    # Mirrors uvicorn's default factory for the daemon: server cert + client-CA + CERT_REQUIRED.
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=ca)
    server_ctx.load_cert_chain(certfile=server_cert, keyfile=server_key)
    server_ctx.verify_mode = ssl.CERT_REQUIRED
    server_ctx.verify_flags |= ssl.VERIFY_X509_STRICT

    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=hostname)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(20):
        for name, obj, outgoing, peer_incoming in (("client", client, c_out, s_in),
                                                   ("server", server, s_out, c_in)):
            if not done[name]:
                try:
                    obj.do_handshake()
                    done[name] = True
                except ssl.SSLWantReadError:
                    pass
            peer_incoming.write(outgoing.read())
        if all(done.values()):
            return
    raise AssertionError(f"handshake did not complete: {done}")


def test_fixture_certs_pass_strict_x509(mtls_certs):
    # The shared fixture must mint certs a 3.13+ host would accept; otherwise every
    # mTLS test is validating material production can no longer use.
    _strict_mtls_handshake(
        ca=mtls_certs.ca,
        server_cert=mtls_certs.server_cert, server_key=mtls_certs.server_key,
        client_cert=mtls_certs.client_cert, client_key=mtls_certs.client_key,
        hostname="localhost",
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not installed")
def test_mint_script_certs_pass_strict_x509(tmp_path):
    out, tls_dir = tmp_path / "out", tmp_path / "tls"
    subprocess.run(
        ["bash", str(MINT_SCRIPT), str(out), "daemon.test.ts.net", "127.0.0.1"],
        env={"PATH": "/usr/bin:/bin", "VIBERPIT_TLS_DIR": str(tls_dir)},
        check=True, capture_output=True, text=True,
    )
    _strict_mtls_handshake(
        ca=str(tls_dir / "ca.pem"),
        server_cert=str(out / "server.pem"), server_key=str(out / "server.key"),
        client_cert=str(out / "client.pem"), client_key=str(out / "client.key"),
        hostname="daemon.test.ts.net",
    )
