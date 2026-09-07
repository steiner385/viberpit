"""Every entrypoint must mirror the env prefixes before it reads a setting (Phase 0b).

The mirror is only a compatibility guarantee if it is unmissable: one entrypoint that
forgets to call it configures itself from defaults on a host whose env file has moved
to VIBERPIT_*, silently reverting CCSWITCH_MASTER_KEY / DB_URL (spec §4 risk 3). This
pins the call sites, so adding an entrypoint without the call fails CI.
"""
import os
import re
from pathlib import Path

from ccswitch import env_compat
from server.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Files that own a process lifetime or build settings, and the call they must make.
ENTRYPOINTS = (
    "server/__init__.py",        # runs before any server.* module body, incl. app.py's create_app()
    "server/ipc/__main__.py",
    "server/deploy/__main__.py",
    "server/codex/auth_main.py",
    "server/db/migrations/env.py",
    "server/config.py",
    "src/ccswitch/cli.py",
    "src/ccswitch/runner.py",
    "src/ccswitch/launcher.py",
)


def test_every_entrypoint_calls_the_mirror():
    missing = [rel for rel in ENTRYPOINTS
               if "mirror_legacy_env()" not in (REPO_ROOT / rel).read_text()]
    assert not missing, f"entrypoints that never mirror the env prefixes: {missing}"


def test_importing_server_has_already_mirrored():
    """server/app.py builds its app at import time, so the mirror cannot wait for a
    lifespan hook — importing the `server` package is what performs it."""
    import server  # noqa: F401  (imported for the side effect under test)

    assert env_compat._mirrored is True


def test_settings_read_a_migrated_env_file():
    """A host that already spells VIBERPIT_* still configures the legacy aliases."""
    fake = dict(os.environ)
    fake.pop("CCSWITCH_HOST", None)
    fake["VIBERPIT_HOST"] = "10.11.12.13"

    original_mirrored = env_compat._mirrored
    original_environ = os.environ
    os.environ = fake  # type: ignore[assignment]
    env_compat._mirrored = False
    try:
        assert get_settings().host == "10.11.12.13"
        assert fake["CCSWITCH_HOST"] == "10.11.12.13"
    finally:
        os.environ = original_environ  # type: ignore[assignment]
        env_compat._mirrored = original_mirrored


def test_shell_scripts_read_both_prefixes():
    """Shell entrypoints run outside any Python process, so they carry the idiom
    themselves: ${VIBERPIT_X:-${CCSWITCH_X:-default}}."""
    legacy_read = re.compile(r"\$\{CCSWITCH_([A-Z0-9_]+)(:?-)")
    offenders = []
    for scan_dir in ("scripts", "deploy"):
        for path in sorted((REPO_ROOT / scan_dir).rglob("*.sh")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for name, _op in legacy_read.findall(line):
                    if f"${{VIBERPIT_{name}" not in line:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} CCSWITCH_{name}")
    assert not offenders, (
        "shell reads of a legacy env name with no VIBERPIT_ fallback: " + ", ".join(offenders))
