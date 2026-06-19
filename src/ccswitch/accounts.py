"""Integration glue: token validity, usage caching, selection, sync-back, registry."""
import time
from datetime import datetime, timezone

from . import configdir, ledger, oauth, paths
from .config import Config, load_config
from .creds import merge_keep_later, read_json, split_credentials, write_json_secure
from .selector import select_best


def _account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    return sorted(p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))


def ensure_valid_token(name: str) -> dict:
    """Return a non-expired claudeAiOauth blob, refreshing + persisting if needed."""
    blob = read_json(paths.slot_path(name))
    cfg = load_config()
    now_ms = int(time.time() * 1000)
    if blob.get("expiresAt", 0) - now_ms > cfg.expiry_skew_sec * 1000:
        return blob
    new_blob = oauth.refresh_access_token(blob["refreshToken"], blob)
    write_json_secure(paths.slot_path(name), new_blob)
    return new_blob


def usage_for(name: str) -> oauth.Usage | None:
    """Cached-or-fetched usage. Returns None and marks needs-reauth if auth fails."""
    cfg = load_config()
    now = datetime.now(timezone.utc)
    with ledger.locked_ledger() as led:
        cached = ledger.cached_usage(led, name, cfg, now)
        if cached is not None:
            return cached

    # Step 1: ensure the token is valid (may refresh if near-expiry).
    # If the refresh token is already dead, mark needs-reauth immediately — do not retry.
    try:
        blob = ensure_valid_token(name)
    except oauth.AuthError:
        with ledger.locked_ledger() as led:
            ledger.set_status(led, name, "needs-reauth")
        return None

    # Step 2: fetch usage; on 401 do exactly ONE refresh+refetch.
    try:
        u = oauth.fetch_usage(blob["accessToken"])
    except oauth.AuthError:
        try:
            slot = read_json(paths.slot_path(name))
            slot = oauth.refresh_access_token(slot["refreshToken"], slot)
            write_json_secure(paths.slot_path(name), slot)
            u = oauth.fetch_usage(slot["accessToken"])
        except oauth.AuthError:
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
            return None

    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, u, now)
        ledger.set_status(led, name, "ok")
    return u


def pick_account(cfg: Config, exclude: set[str] = frozenset()) -> str:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        name = select_best(usages, reserved, cfg, now, exclude)
        ledger.reserve(led, name, now_ts + cfg.reservation_ttl_sec)
    return name


def sync_back(name: str) -> None:
    """Split the live config-dir creds back into the per-account slot + shared mcp store."""
    live_path = paths.account_cfg_dir(name) / ".credentials.json"
    if not live_path.exists():
        return
    claude_oauth, mcp = split_credentials(read_json(live_path))
    existing = read_json(paths.slot_path(name)) if paths.slot_path(name).exists() else None
    write_json_secure(paths.slot_path(name), merge_keep_later(existing, claude_oauth))
    if mcp:
        write_json_secure(paths.shared_mcp_path(), mcp)


def remove_account(name: str) -> None:
    import shutil
    paths.slot_path(name).unlink(missing_ok=True)
    cfg_dir = paths.account_cfg_dir(name)
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir)


def list_accounts() -> list[dict]:
    out = []
    with ledger.locked_ledger() as led:
        for name in _account_names():
            acct = led["accounts"].get(name, {})
            out.append({"name": name, "status": acct.get("status", "unknown"), "usage": acct.get("usage")})
    return out


def add_account(name: str) -> None:
    """Capture-on-login: run `claude /login` against a temp capture dir, then store the creds."""
    import os
    import shlex
    import subprocess
    import tempfile
    from .config import load_config
    cfg = load_config()
    claude_cmd = shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))
    with tempfile.TemporaryDirectory() as capture:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=capture)
        print(f"Log in to the '{name}' account in the browser window that opens…")
        subprocess.run(claude_cmd + ["/login"], env=env)
        cred_path = os.path.join(capture, ".credentials.json")
        if not os.path.exists(cred_path):
            raise RuntimeError("login did not produce credentials; aborting")
        claude_oauth, mcp = split_credentials(read_json(cred_path))
        if claude_oauth.get("subscriptionType") != "max":
            print(f"WARNING: '{name}' is not a Max subscription (got {claude_oauth.get('subscriptionType')!r})")
        write_json_secure(paths.slot_path(name), claude_oauth)
        if mcp:
            existing = read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
            existing.update(mcp)
            write_json_secure(paths.shared_mcp_path(), existing)
    configdir.compose(name)
