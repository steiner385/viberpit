"""Entrypoint for the credential authority: ``python -m server.codex.auth_main``.

Run by ``deploy/ccswitch-codex-auth.service`` as the ``ccswitch-auth`` user. This
is the only module in ``server/codex/`` that both reads the environment and holds
a process lifetime, so it is where every fail-closed startup check lives:

1. Configuration is loaded from ``CCSWITCH_CODEX_AUTHORITY_*`` and any missing,
   unreadable, or non-mTLS value aborts the start.
2. The instance lock is taken before anything is served. A second authority dies
   rather than becoming a second writer of the generation counter.
3. Only then is the listener bound.

There is no degraded mode and no "start anyway and log a warning" branch. A
credential authority that cannot verify its clients has no safe behaviour.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from ccswitch.env_compat import mirror_legacy_env
from server.codex import auth_endpoint, process as codex_process
from server.codex.auth_authority import CredentialAuthority, InstanceLock
from server.codex.auth_server import AuthorityServer
from server.codex.auth_store import CredentialStore
from server.codex.auth_types import AuthorityError
from server.obs.context import log_event

logger = logging.getLogger("server.codex.auth_main")


async def run() -> int:
    try:
        endpoint = auth_endpoint.listen_endpoint()
        tls = auth_endpoint.authority_tls_files()
        allowlist = auth_endpoint.client_allowlist()
        state_dir = auth_endpoint.state_dir()
    except auth_endpoint.AuthorityConfigError as exc:
        logger.error("codex credential authority cannot start: %s", exc)
        return 2

    availability = await codex_process.probe_availability(
        enabled=True,
        # The authority itself uses managed auth, but it still depends on the
        # exact app-server schema containing the external-token methods served to
        # runners. Reuse the runner's pinned version/schema probe verbatim.
        external_auth_enabled=True,
        binary=os.environ.get(
            "CCSWITCH_CODEX_AUTHORITY_BIN", codex_process.CODEX_EXE
        ),
    )
    if not availability.available or not availability.binary_path:
        reason = availability.reason.value if availability.reason is not None else "unknown"
        logger.error(
            "codex credential authority cannot start: native boundary %s (%s)",
            reason,
            availability.detail,
        )
        return 4

    lock = InstanceLock(state_dir / "authority.lock")
    try:
        lock.acquire()
    except AuthorityError as exc:
        logger.error("codex credential authority cannot start: %s", exc)
        return 3

    store = CredentialStore(state_dir)
    authority = CredentialAuthority(store=store, binary=availability.binary_path)
    server = AuthorityServer(authority, allowlist=allowlist)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        assert endpoint.host is not None and endpoint.port is not None
        await server.start(
            endpoint.host, endpoint.port, auth_endpoint.server_ssl_context(tls)
        )
        log_event(
            logger,
            logging.INFO,
            "codex.auth.started",
            endpoint=str(endpoint),
            state_dir=str(state_dir),
            allowlist=sorted(allowlist),
        )
        await stop.wait()
    finally:
        # Order matters on the way down too: stop accepting first, so no new call
        # can start a login while we are cancelling the ones already running.
        await server.aclose()
        await authority.aclose()
        lock.release()
        log_event(logger, logging.INFO, "codex.auth.stopped")
    return 0


def main() -> int:
    from server.obs.jsonlog import configure_logging

    mirror_legacy_env()  # VIBERPIT_* <-> CCSWITCH_*, before any setting is read
    configure_logging()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover -- exercised by the systemd unit
    sys.exit(main())
