"""Endpoint + mTLS material for the ChatGPT credential authority.

Deliberately a sibling of :mod:`server.ipc.paths` and :mod:`server.ipc.tls` rather
than a reuse of them. The session daemon's key material and its
``CCSWITCH_SESSIOND_*`` variables address a *different* trust domain: the daemon
runs as the session user and its client key is held by the edge. The authority
runs as ``ccswitch-auth`` and holds ChatGPT refresh tokens, so an operator who
leaks the daemon's client key must not thereby be able to ask the authority for
an access token. Sharing the loader would make that separation a convention;
separate variables make it a configuration fact, and the assertion in
:func:`assert_distinct_from_sessiond` makes it a startup check.

Everything here reads the environment. Nothing else under ``server/codex/``
does — the manager, the authority and the client all take their configuration as
arguments, so the same code is drivable from a test with no environment at all.
"""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from server.ipc.paths import Endpoint, parse_endpoint

#: Where the authority keeps managed ``CODEX_HOME`` directories and its lock.
DEFAULT_STATE_DIR: Final = "/var/lib/ccswitch-codex-auth"

#: Certificate common name the edge presents. One identity, one privilege set.
EDGE_IDENTITY: Final = "codex-edge"
#: Prefix of the identity every runner presents: ``codex-runner:<host>``.
RUNNER_IDENTITY_PREFIX: Final = "codex-runner:"

#: Total budget for one runner→authority refresh, including connect, write and
#: read. The app-server is holding a turn open behind this, so it is short.
DEFAULT_REFRESH_DEADLINE_S: Final = 8.0
#: Budget for administrative calls (login start/status/cancel, sync, revoke).
#: Longer than a refresh: an operator is watching, and no turn is blocked.
DEFAULT_ADMIN_DEADLINE_S: Final = 20.0


class AuthorityConfigError(RuntimeError):
    """Required authority configuration is missing, unreadable, or not mTLS.

    Always fatal at startup. There is no degraded mode: an authority that cannot
    verify its clients, or a runner that cannot verify the authority, must not
    run at all — the fallback would be "trust whoever connects" on one side and
    "accept credentials from whoever answers" on the other.
    """


@dataclass(frozen=True, slots=True)
class AuthorityTLSFiles:
    """Server-side material: the cert the authority presents + the client CA."""

    cert: str
    key: str
    client_ca: str


@dataclass(frozen=True, slots=True)
class AuthorityClientTLSFiles:
    """Client-side material: our cert/key + the CA that signs the authority's."""

    cert: str
    key: str
    ca: str


def _require(value: str | None, what: str) -> str:
    if not value:
        raise AuthorityConfigError(f"the codex credential authority requires {what}")
    if not os.access(value, os.R_OK):
        raise AuthorityConfigError(f"{what} is not readable: {value}")
    return value


def state_dir() -> Path:
    return Path(os.environ.get("CCSWITCH_CODEX_AUTHORITY_STATE_DIR") or DEFAULT_STATE_DIR)


def _endpoint(raw: str | None, what: str) -> Endpoint:
    if not raw:
        raise AuthorityConfigError(f"the codex credential authority requires {what}")
    try:
        endpoint = parse_endpoint(raw)
    except ValueError as exc:
        raise AuthorityConfigError(f"{what}: {exc}") from None
    if not endpoint.is_tcp:
        raise AuthorityConfigError(
            f"{what} must be an https:// endpoint — the authority is mutually "
            f"authenticated across hosts and has no unix-socket mode"
        )
    return endpoint


def listen_endpoint() -> Endpoint:
    """Where the authority binds. ``https://host:port`` only, never a socket."""
    return _endpoint(
        os.environ.get("CCSWITCH_CODEX_AUTHORITY_LISTEN_ENDPOINT"),
        "CCSWITCH_CODEX_AUTHORITY_LISTEN_ENDPOINT",
    )


def connect_endpoint() -> Endpoint:
    """Where a client (edge or runner) reaches the authority."""
    return _endpoint(
        os.environ.get("CCSWITCH_CODEX_AUTHORITY_ENDPOINT"),
        "CCSWITCH_CODEX_AUTHORITY_ENDPOINT",
    )


def authority_configured() -> bool:
    """True when a client endpoint is configured at all.

    The *only* soft check in this module: it answers "should Codex external auth
    be attempted on this host?", so that a host with Codex disabled does not log a
    configuration error on every boot. Once it is true, every other loader here is
    strict.
    """
    return bool(os.environ.get("CCSWITCH_CODEX_AUTHORITY_ENDPOINT"))


def authority_tls_files() -> AuthorityTLSFiles:
    files = AuthorityTLSFiles(
        cert=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_TLS_CERT"),
            "CCSWITCH_CODEX_AUTHORITY_TLS_CERT",
        ),
        key=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_TLS_KEY"),
            "CCSWITCH_CODEX_AUTHORITY_TLS_KEY",
        ),
        client_ca=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_TLS_CLIENT_CA"),
            "CCSWITCH_CODEX_AUTHORITY_TLS_CLIENT_CA",
        ),
    )
    assert_distinct_from_sessiond(files.key, "CCSWITCH_CODEX_AUTHORITY_TLS_KEY")
    return files


def authority_client_tls_files() -> AuthorityClientTLSFiles:
    files = AuthorityClientTLSFiles(
        cert=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT"),
            "CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT",
        ),
        key=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY"),
            "CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY",
        ),
        ca=_require(
            os.environ.get("CCSWITCH_CODEX_AUTHORITY_CLIENT_CA"),
            "CCSWITCH_CODEX_AUTHORITY_CLIENT_CA",
        ),
    )
    assert_distinct_from_sessiond(files.key, "CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY")
    return files


def assert_distinct_from_sessiond(key_path: str, what: str) -> None:
    """Refuse to reuse the session daemon's private keys for the authority.

    The plan states it as a rule ("never reuse the session-daemon server key or
    edge session-RPC client key"); this makes it a startup failure rather than a
    review comment, because the misconfiguration is invisible once running — the
    handshake succeeds and the trust domains are silently merged.
    """
    resolved = os.path.realpath(key_path)
    for var in (
        "CCSWITCH_SESSIOND_TLS_KEY",
        "CCSWITCH_SESSIOND_CLIENT_KEY",
    ):
        other = os.environ.get(var)
        if other and os.path.realpath(other) == resolved:
            raise AuthorityConfigError(
                f"{what} must not be the same key as {var}: the credential "
                f"authority is a separate trust domain from the session daemon"
            )


def client_allowlist() -> frozenset[str]:
    """Certificate identities the authority will complete a handshake with.

    Unlike ``server.ipc.tls.client_allowlist`` — which returns ``None`` for
    "unset" and is then permissive — an unset allowlist here is a hard error.
    Permissive is a defensible default for a Unix-socket-or-mTLS transport whose
    peers are already inside the tailnet; it is not defensible for the one service
    that hands out ChatGPT access tokens.
    """
    raw = os.environ.get("CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST") or ""
    items = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise AuthorityConfigError(
            "the codex credential authority requires a non-empty "
            "CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST"
        )
    return items


def refresh_deadline_s() -> float:
    return _positive_float(
        "CCSWITCH_CODEX_AUTH_REFRESH_DEADLINE_S", DEFAULT_REFRESH_DEADLINE_S
    )


def admin_deadline_s() -> float:
    return _positive_float(
        "CCSWITCH_CODEX_AUTH_ADMIN_DEADLINE_S", DEFAULT_ADMIN_DEADLINE_S
    )


def _positive_float(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise AuthorityConfigError(f"{var} must be a number, got {raw!r}") from None
    if value <= 0:
        raise AuthorityConfigError(f"{var} must be positive, got {value}")
    return value


def server_ssl_context(files: AuthorityTLSFiles) -> ssl.SSLContext:
    """The authority's listening context. Mutual TLS is not optional here.

    ``CERT_REQUIRED`` plus a client CA is what makes ``getpeercert()`` in the
    connection handler non-empty and trustworthy — the per-route ACL is built
    directly on it, so a context that allowed an anonymous client would silently
    turn every ACL into "allow".
    """
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=files.client_ca)
    ctx.load_cert_chain(certfile=files.cert, keyfile=files.key)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def client_ssl_context(files: AuthorityClientTLSFiles) -> ssl.SSLContext:
    """A client's context: verify the authority's cert, present our own."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=files.ca)
    ctx.load_cert_chain(certfile=files.cert, keyfile=files.key)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
