"""Run the session daemon: `python -m server.ipc [socket_path]` (default: well-known path)."""
import sys

from viberpit.env_compat import mirror_legacy_env
from server.startup_guard import make_non_dumpable, refuse_empty_secrets

if __name__ == "__main__":
    mirror_legacy_env()  # mirrors the legacy env prefix onto the current one, before any setting is read
    # #3386: never boot on a session's blanked env, and keep /proc/<pid>/environ, ptrace
    # and core dumps root-only — both before the daemon's modules read any setting.
    refuse_empty_secrets(process="session daemon")
    make_non_dumpable()
    from server.ipc.serve import serve
    serve(sys.argv[1] if len(sys.argv) > 1 else None)
