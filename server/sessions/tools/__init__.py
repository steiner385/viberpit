"""The coding-session in-process MCP surface (session tools spec §3).

Tools close over a :class:`SessionToolContext` inside the daemon that owns the session
and act on this session directly — no loopback HTTP, no token — so they work on any
fleet host. Importing the package registers the surface with ``server.mcp_spec``.
"""
from . import registry as _registry  # noqa: F401 - registers the "session" surface
