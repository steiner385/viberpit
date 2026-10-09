"""MCP server naming shared by the core (creds) and the server (status store, auth, library)."""
from __future__ import annotations


def short_name(server_key: str) -> str:
    """Strip the ``plugin:<p>:`` prefix: ``plugin:railway:railway`` -> ``railway``."""
    return server_key.rsplit(":", 1)[-1] if server_key.startswith("plugin:") else server_key
