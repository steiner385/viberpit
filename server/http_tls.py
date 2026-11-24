"""The ONE CA-verifying ``SSLContext`` every short-lived httpx client in a process shares.

httpx builds a new ``SSLContext`` for every ``Client``/``HTTPTransport`` that is not
handed one, loading the whole certifi CA bundle into it — including ``httpx.get()``,
which is a throwaway client, and a ``uds=`` transport, which never speaks TLS at all.
Under OpenSSL 3.5 + glibc, every such client that makes a request leaves ~0.7 MB of
freed heap that glibc never reuses or returns: in-use memory stays flat (tracemalloc
sees nothing) while the heap grows without bound. The deploy controller built one per
30 s detect tick and reached VmSize 4.7 GB, 4.2 GB of it swapped out, in 11 days
(dobby, 2026-11-24). ``server/tests/test_deploy_memory_bounded.py`` measures it.

So a client that is created per call passes ``verify=shared_ssl_context()``. A context
is safe to share across clients, threads and event loops; it is built once, lazily, by
``httpx.create_ssl_context`` itself, so ``SSL_CERT_FILE``/``SSL_CERT_DIR`` and the
certifi default behave exactly as they would with ``verify=True``.

A context with a *different* trust root (the daemon mTLS context from
``server.ipc.tls.client_ssl_context``) is not this one and stays separate.
"""
from __future__ import annotations

import functools
import ssl

import httpx


@functools.cache
def shared_ssl_context() -> ssl.SSLContext:
    """The process-wide default-verification context (certifi, or SSL_CERT_FILE/DIR)."""
    return httpx.create_ssl_context()
