# ViberPit web backend
#
# The env-prefix mirror (legacy prefix onto the current one) runs here rather than in
# server/app.py because `app = create_app()` executes at *import* time and many
# server modules read os.environ in their module bodies. Python runs this file
# before any `server.*` module body, so this is the earliest point a server
# process can mirror — strictly earlier than app.py could. It is idempotent, so
# the per-entrypoint calls (server/ipc, server/deploy, server/codex.auth_main,
# alembic env.py, Settings) are cheap no-ops after it.
#
# For the same reason it is the earliest point a systemd unit's main process (the edge
# under uvicorn, the daemon, the deploy controller) can make itself non-dumpable, before
# any module reads a secret (#3386, server/startup_guard.py). Only the unit's main
# process: a child that merely inherits the unit's env stays dumpable.
from viberpit.env_compat import mirror_legacy_env

mirror_legacy_env()

from server.startup_guard import harden_if_service  # noqa: E402

harden_if_service()
