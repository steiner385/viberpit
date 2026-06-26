"""TLS material + SSL contexts for the cross-host (mTLS) daemon transport.

Only consulted when the daemon endpoint scheme is `https`. For `unix://` the trust
boundary is the socket file's permissions and none of this is used.
"""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass


class TLSConfigError(RuntimeError):
    """A required TLS file is missing/unreadable for an https endpoint."""


@dataclass(frozen=True)
class DaemonTLSFiles:
    cert: str   # server cert the daemon presents
    key: str    # server private key
    ca: str     # CA used to verify CLIENT certs


@dataclass(frozen=True)
class ClientTLSFiles:
    cert: str   # client cert the edge presents
    key: str    # client private key
    ca: str     # CA used to verify the SERVER cert


def _require(path: str | None, what: str) -> str:
    if not path:
        raise TLSConfigError(f"https daemon endpoint requires {what} (unset)")
    if not os.access(path, os.R_OK):
        raise TLSConfigError(f"{what} not readable: {path}")
    return path


def daemon_tls_files() -> DaemonTLSFiles:
    return DaemonTLSFiles(
        cert=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_CERT"), "CCSWITCH_SESSIOND_TLS_CERT"),
        key=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_KEY"), "CCSWITCH_SESSIOND_TLS_KEY"),
        ca=_require(os.environ.get("CCSWITCH_SESSIOND_TLS_CA"), "CCSWITCH_SESSIOND_TLS_CA"),
    )


def client_tls_files() -> ClientTLSFiles:
    return ClientTLSFiles(
        cert=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_CERT"), "CCSWITCH_SESSIOND_CLIENT_CERT"),
        key=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_KEY"), "CCSWITCH_SESSIOND_CLIENT_KEY"),
        ca=_require(os.environ.get("CCSWITCH_SESSIOND_CLIENT_CA"), "CCSWITCH_SESSIOND_CLIENT_CA"),
    )


def client_ssl_context(files: ClientTLSFiles) -> ssl.SSLContext:
    """SSL context the edge uses for httpx (verify=) AND websockets (ssl=).

    Trusts the configured CA for the server cert and presents the client cert chain.
    check_hostname stays True — the client verifies the daemon's cert SAN.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=files.ca)
    ctx.load_cert_chain(certfile=files.cert, keyfile=files.key)
    return ctx
