"""Reauth profile preparation and cleanup for the account-reauthenticator flow."""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from . import configdir, paths
from .creds import read_json, split_credentials, write_credentials, write_json_secure

log = logging.getLogger(__name__)

_STRIP_SQL = "DELETE FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'"

# Extensions carried into the temp login profile. 1Password by default: with its
# files + state copied, the user unlocks with the master password inside the reauth
# window instead of typing credentials by hand. Override: CCSWITCH_REAUTH_EXTENSIONS.
REAUTH_EXTENSION_IDS: tuple[str, ...] = ("aeblfdkhhhdcdjpifhhbdiojplfjncoa",)

# Registrable domains whose cookies are carried into the temp login profile, as an
# ALLOWLIST — empty by default, i.e. no cookies travel at all (#1105).
#
# The OAuth login starts signed out, so it needs no cookies of its own. Carrying the
# source jar re-presented the operator's live Google/Gmail session from a fresh
# profile with a new fingerprint, several times within a couple of minutes, which is
# exactly the shape session-risk heuristics score as a hijack — and the observed
# result was their unrelated Google session being invalidated. An allowlist also
# fails safe: a domain nobody enumerated is dropped, where the old `%claude%`
# denylist carried it over. Opt in per domain with CCSWITCH_REAUTH_COOKIE_HOSTS.
REAUTH_COOKIE_HOSTS: tuple[str, ...] = ()


def _reauth_root() -> Path:
    """Ensure and return the reauth-tmp root directory (mode 0700)."""
    r = paths.reauth_tmp_dir()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    return r


def _under_reauth_tmp(path: str) -> bool:
    """Check if path is under reauth-tmp and exists."""
    try:
        return Path(path).resolve(strict=True).is_relative_to(paths.reauth_tmp_dir().resolve())
    except (OSError, ValueError):
        return False


def session_bus_env(
    environ: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
    run_root: str = "/run/user",
) -> dict[str, str]:
    """Env vars a keyring-backed Chrome needs when launched from a bus-less service.

    The reauth Chrome runs ``--password-store=gnome-libsecret`` so it can decrypt the
    v11 cookies copied from the real profile. systemd services do not inherit the
    user's session bus, so without these two variables Chrome cannot reach
    gnome-keyring and silently drops every keyring-encrypted cookie — Google looks
    logged out. Returns only the variables *environ* lacks; never overrides.
    """
    env = os.environ if environ is None else environ
    uid = os.getuid() if uid is None else uid
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"{run_root}/{uid}"
    bus = Path(runtime_dir) / "bus"
    if not bus.is_socket():
        return {}
    out: dict[str, str] = {}
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        out["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    if "XDG_RUNTIME_DIR" not in env:
        out["XDG_RUNTIME_DIR"] = runtime_dir
    return out


def _filter_cookies(db: Path, allow_hosts: Sequence[str]) -> None:
    """Reduce a copied cookie jar to *allow_hosts*, minus Claude/Anthropic.

    A host matches when ``host_key`` equals it or is a dot-suffixed subdomain of it,
    so ``google.com`` keeps ``.google.com`` and ``accounts.google.com`` but not
    ``evilgoogle.com``. Claude/Anthropic rows are dropped even when allowlisted —
    the whole point of the temp profile is to be signed OUT of Claude so
    ``claude auth login`` presents a fresh login.

    VACUUMs afterwards: a plain DELETE leaves the row bytes in SQLite freelist
    pages, so a "stripped" copy would still hand over the cookies it dropped.
    """
    conn = sqlite3.connect(str(db))
    try:
        if not allow_hosts:
            conn.execute("DELETE FROM cookies")
        else:
            clauses = " OR ".join(["(host_key = ? OR host_key LIKE ?)"] * len(allow_hosts))
            params: list[str] = []
            for host in allow_hosts:
                params += [host, f"%.{host}"]
            conn.execute(f"DELETE FROM cookies WHERE NOT ({clauses})", params)
            conn.execute(_STRIP_SQL)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _copy_extensions(src: Path, default: Path, extension_ids: Sequence[str]) -> None:
    """Copy the curated extension set and register it in a MINIMAL Preferences.

    Copies, per id that exists both on disk (Extensions/<id>) and in the source
    Preferences' extensions.settings: Extensions/<id> and Local Extension Settings/<id>.
    Once: Extension State/. Then writes Default/Preferences containing ONLY
    {"extensions": {"settings": {<id>: <entry>}}}.

    Why minimal: copying the full Preferences names the signed-in Google account whose
    refresh token lives in the (uncopied) Web Data, and Chrome's account reconciler then
    logs the Gaia cookies out — undoing the whole point of the cookie copy.
    Anything missing is skipped at INFO; this must never fail the login (spec D4).
    """
    if not extension_ids:
        return
    prefs_path = src / "Preferences"
    try:
        parsed = json.loads(prefs_path.read_text())
    except (OSError, ValueError) as e:
        log.info("reauth profile: source Preferences unreadable (%s); skipping extensions", e)
        return
    extensions = parsed.get("extensions") if isinstance(parsed, dict) else None
    settings_obj = extensions.get("settings") if isinstance(extensions, dict) else None
    registered = settings_obj if isinstance(settings_obj, dict) else {}
    settings: dict[str, object] = {}
    for ext_id in extension_ids:
        ext_dir = src / "Extensions" / ext_id
        entry = registered.get(ext_id)
        if not ext_dir.is_dir() or entry is None:
            log.info("reauth profile: extension %s not installed in source; skipping", ext_id)
            continue
        dst_ext_dir = default / "Extensions" / ext_id
        try:
            shutil.copytree(ext_dir, dst_ext_dir, ignore_dangling_symlinks=True)
        except (OSError, shutil.Error) as e:
            # A half-copied Extensions/<id> hands Chrome a corrupt extension —
            # worse than no 1Password at all — so the id must NOT be registered.
            log.info(
                "reauth profile: copying extension %s failed (%s); skipping id", ext_id, e
            )
            shutil.rmtree(dst_ext_dir, ignore_errors=True)
            continue
        les = src / "Local Extension Settings" / ext_id
        if les.is_dir():
            try:
                shutil.copytree(
                    les, default / "Local Extension Settings" / ext_id,
                    ignore_dangling_symlinks=True,
                )
            except (OSError, shutil.Error) as e:
                # Live LevelDB store the running Chrome compacts continuously —
                # a failed copy here only loses saved state, not the extension
                # itself, so the id stays registered (D4: never fatal).
                log.info(
                    "reauth profile: copying Local Extension Settings for %s failed (%s); "
                    "skipping saved state", ext_id, e
                )
                shutil.rmtree(default / "Local Extension Settings" / ext_id, ignore_errors=True)
        settings[ext_id] = entry
    if not settings:
        return
    state = src / "Extension State"
    if state.is_dir():
        try:
            shutil.copytree(state, default / "Extension State", ignore_dangling_symlinks=True)
        except (OSError, shutil.Error) as e:
            log.info("reauth profile: copying Extension State failed (%s); skipping", e)
            shutil.rmtree(default / "Extension State", ignore_errors=True)
    dst_prefs = default / "Preferences"
    dst_prefs.write_text(json.dumps({"extensions": {"settings": settings}}))
    os.chmod(dst_prefs, 0o600)


def prepare_reauth_profile(
    source_profile_dir: str,
    *,
    extension_ids: Sequence[str] | None = None,
    cookie_hosts: Sequence[str] | None = None,
    copy_login_data: bool = False,
) -> str:
    """Build a throwaway Chrome profile for one login, carrying as little as possible.

    Nothing sensitive travels by default (#1105): no cookies, no ``Local State`` key
    material, no saved passwords. Only the curated extension set is copied (see
    ``_copy_extensions``), and Claude is signed out either way. Each sensitive
    artifact is opt-in and pulls in only what it needs:

    * ``cookie_hosts`` non-empty → the source jar is copied, reduced to those hosts
      (``_filter_cookies``), and ``Local State`` comes along so Chrome can decrypt
      the v11 rows.
    * ``copy_login_data`` → the saved-password database is copied, which also needs
      ``Local State``. An OAuth login does not require it; it exists only for
      operators who fill the form from Chrome's own password manager.

    Args:
        source_profile_dir: Path to a Chrome profile directory. Defaults to the
            dedicated ccswitch profile, NOT the operator's everyday one.
        extension_ids: Extension ids to carry over. None → REAUTH_EXTENSION_IDS; () → none.
        cookie_hosts: Domains whose cookies to carry. None → REAUTH_COOKIE_HOSTS
            (empty, i.e. none); () → none.
        copy_login_data: Copy the source's saved-password database.

    Returns:
        Path to the temp profile directory (under reauth-tmp)

    Raises:
        FileNotFoundError: If cookies were requested but the source Cookies database
            is missing. With no cookies requested a missing/absent source profile is
            fine — the temp profile simply starts empty.
    """
    src = Path(source_profile_dir)
    hosts = list(REAUTH_COOKIE_HOSTS if cookie_hosts is None else cookie_hosts)
    src_cookies = src / "Cookies"
    if hosts and not src_cookies.exists():
        raise FileNotFoundError(f"chrome profile Cookies not found: {src_cookies}")

    # Create temp dir under reauth-tmp with 0700 perms
    old_umask = os.umask(0o077)
    try:
        temp = tempfile.mkdtemp(prefix="reauth-", dir=str(_reauth_root()))
        default = Path(temp) / "Default"
        default.mkdir()
    finally:
        os.umask(old_umask)
    os.chmod(temp, 0o700)
    os.chmod(default, 0o700)

    # Anything that raises past this point has already created a directory holding
    # whatever was copied so far, and the caller gets no path to clean up — so own
    # the teardown here rather than leaving a partial profile behind (#1106).
    try:
        if hosts:
            dst_cookies = default / "Cookies"
            shutil.copy(src_cookies, dst_cookies)
            os.chmod(dst_cookies, 0o600)
            _filter_cookies(dst_cookies, hosts)

        # Saved passwords (v11-encrypted like Cookies; same keyring, same trust class).
        if copy_login_data:
            login_data = src / "Login Data"
            if login_data.exists():
                dst_ld = default / "Login Data"
                shutil.copy(login_data, dst_ld)
                os.chmod(dst_ld, 0o600)
            else:
                log.info("reauth profile: no Login Data in source; skipping")

        # Local State holds the key that decrypts the two artifacts above; with
        # neither copied there is nothing for it to unlock, so it stays behind.
        if hosts or copy_login_data:
            local_state = src.parent / "Local State"
            if local_state.exists():
                dst_ls = Path(temp) / "Local State"
                shutil.copy(local_state, dst_ls)
                os.chmod(dst_ls, 0o600)

        _copy_extensions(
            src, default, REAUTH_EXTENSION_IDS if extension_ids is None else extension_ids
        )
    except BaseException:
        cleanup_reauth_profile(temp)
        raise

    return temp


def cleanup_reauth_profile(path: str) -> None:
    """Remove a temp reauth profile directory.

    Only removes if the path is under reauth-tmp (safety guard).

    Args:
        path: Path to the temp profile directory
    """
    if _under_reauth_tmp(path):
        shutil.rmtree(path, ignore_errors=True)


def capture_login(
    name: str,
    *,
    env_extra: dict | None = None,
    claude_bin: Sequence[str] | None = None,
    extra_login_args: Sequence[str] | None = None,
) -> bool:
    """Run `claude auth login --claudeai` in a capture dir under reauth-tmp and store creds.

    The capture CLAUDE_CONFIG_DIR is created under `~/.config/ccswitch/reauth-tmp/`
    (never /tmp) and is cleaned up in a try/finally regardless of success or failure.

    The captured credential blob is NEVER logged or printed.

    Args:
        name: Account slot name to store credentials under.
        env_extra: Additional environment variables to merge (e.g. BROWSER, DISPLAY).
            Applied last, so they override the injected session-bus variables.
        claude_bin: Override the claude command argv list (default: ["claude"]).

    Returns:
        True if credentials were captured and stored; False otherwise.
    """
    from .config import load_config

    if claude_bin is None:
        cfg = load_config()

        claude_cmd: list[str] = shlex.split(
            os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
        )
    else:
        claude_cmd = list(claude_bin)

    login_args = ["auth", "login", "--claudeai"] + list(extra_login_args or [])

    # Create capture dir under reauth-tmp (NOT /tmp)
    capture_dir = tempfile.mkdtemp(prefix="capture-", dir=str(_reauth_root()))
    os.chmod(capture_dir, 0o700)
    try:
        # Service-launched (no session bus) → Chrome can't reach gnome-keyring and drops
        # every v11 cookie copied from the real profile. Inject the user's bus so the
        # copied Google session survives; explicit env_extra always wins.
        env = {
            **os.environ,
            **session_bus_env(),
            "CLAUDE_CONFIG_DIR": capture_dir,
            **(env_extra or {}),
        }
        subprocess.run(claude_cmd + login_args, env=env, shell=False)

        cred_path = os.path.join(capture_dir, ".credentials.json")
        if not os.path.exists(cred_path):
            return False

        # Extract and store credentials — blob is never logged
        full_blob = read_json(Path(cred_path))
        claude_oauth, mcp = split_credentials(full_blob)

        paths.store_dir().mkdir(parents=True, exist_ok=True)
        write_credentials(paths.slot_path(name), claude_oauth)

        # Warn if not a Max subscription
        sub_type = claude_oauth.get("subscriptionType")
        if sub_type != "max":
            log.warning("account %s is not a Max subscription (got %r)", name, sub_type)

        if mcp:
            existing = (
                read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
            )
            existing.update(mcp)
            write_json_secure(paths.shared_mcp_path(), existing)

        # Capture per-account .claude.json (identity + caches)
        captured_claude_json = os.path.join(capture_dir, ".claude.json")
        if os.path.exists(captured_claude_json):
            claude_json_data = read_json(Path(captured_claude_json))
            write_json_secure(paths.store_claude_json_path(name), claude_json_data)
            # merge shareable keys into claude.shared.json (deferred import to avoid cycle)
            from .accounts import _merge_shared_claude_json

            _merge_shared_claude_json(claude_json_data)

        configdir.compose(name)
        return True

    finally:
        cleanup_reauth_profile(capture_dir)

