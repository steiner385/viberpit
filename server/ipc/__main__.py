"""Run the session daemon: `python -m server.ipc [socket_path]` (default: well-known path)."""
import sys

from viberpit.env_compat import mirror_legacy_env
from server.ipc.serve import serve

if __name__ == "__main__":
    mirror_legacy_env()  # mirrors the legacy env prefix onto the current one, before any setting is read
    serve(sys.argv[1] if len(sys.argv) > 1 else None)
