# ccswitch web backend
#
# The env-prefix mirror (VIBERPIT_* <-> CCSWITCH_*) runs here rather than in
# server/app.py because `app = create_app()` executes at *import* time and many
# server modules read os.environ in their module bodies. Python runs this file
# before any `server.*` module body, so this is the earliest point a server
# process can mirror — strictly earlier than app.py could. It is idempotent, so
# the per-entrypoint calls (server/ipc, server/deploy, server/codex.auth_main,
# alembic env.py, Settings) are cheap no-ops after it.
from ccswitch.env_compat import mirror_legacy_env

mirror_legacy_env()
