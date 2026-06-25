"""ccswitch: account store management (add / remove / list / link / use / crypto)."""
import argparse
import shlex
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import accounts, configdir
from .config import load_config
from .selector import AllAccountsBusy


def _fmt_reset(iso: str | None, tz: str) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%a %-I:%M %p %Z")


def _cmd_crypto(args: argparse.Namespace) -> int:
    """Handle the `ccswitch crypto` subcommand family."""
    from . import paths
    from .crypto import (
        generate_key,
        is_encrypted,
        key_available,
        master_key,
        store_key_in_keyring,
    )
    from .creds import read_credentials, write_credentials

    sub = args.crypto_cmd

    if sub == "status":
        # Determine key source without printing the key.
        import os
        if os.environ.get("CCSWITCH_MASTER_KEY"):
            source = "env (CCSWITCH_MASTER_KEY)"
        elif master_key() is not None:
            source = "keyring (secret-tool)"
        else:
            source = "none"

        if source == "none":
            print("ccswitch crypto: no master key configured — encryption DISABLED")
        else:
            print(f"ccswitch crypto: key available via {source}")

        # Count encrypted vs plaintext store slots.
        store = paths.store_dir()
        if store.exists():
            enc = plain = 0
            for slot in store.glob("*.cred.json"):
                try:
                    from .creds import read_json
                    data = read_json(slot)
                    rt = data.get("refreshToken", "")
                    if is_encrypted(rt):
                        enc += 1
                    else:
                        plain += 1
                except Exception:
                    pass
            print(f"ccswitch crypto: {enc} slot(s) encrypted, {plain} slot(s) plaintext")
        else:
            print("ccswitch crypto: no store slots found")
        return 0

    if sub == "init":
        if key_available():
            print("ccswitch crypto: a master key is already configured — nothing to do.")
            return 0
        key = generate_key()
        try:
            store_key_in_keyring(key)
        except RuntimeError as exc:
            print(f"ccswitch crypto init: failed to store key in keyring: {exc}", file=sys.stderr)
            return 1
        print("ccswitch crypto: master key generated and stored in keyring.")
        print("ccswitch crypto: run `ccswitch crypto migrate` to encrypt existing slots.")
        return 0

    if sub == "migrate":
        if not key_available():
            print(
                "ccswitch crypto migrate: no master key configured — cannot encrypt.\n"
                "Run `ccswitch crypto init` first.",
                file=sys.stderr,
            )
            return 1
        store = paths.store_dir()
        if not store.exists():
            print("ccswitch crypto migrate: no store slots found.")
            return 0
        migrated = skipped = errors = 0
        for slot in sorted(store.glob("*.cred.json")):
            name = slot.name[: -len(".cred.json")]
            try:
                from .creds import read_json
                raw = read_json(slot)
                if is_encrypted(raw.get("refreshToken", "")):
                    skipped += 1
                    continue
                # read_credentials decrypts (pass-through for plaintext)
                data = read_credentials(slot)
                # write_credentials re-encrypts
                write_credentials(slot, data)
                migrated += 1
            except Exception as exc:
                print(f"ccswitch crypto migrate: error on {name}: {exc}", file=sys.stderr)
                errors += 1
        print(
            f"ccswitch crypto migrate: {migrated} slot(s) encrypted, "
            f"{skipped} already encrypted, {errors} error(s)."
        )
        return 0 if errors == 0 else 1

    # Unknown crypto sub-subcommand (shouldn't reach here due to argparse)
    print(f"ccswitch crypto: unknown sub-command {sub!r}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccswitch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--no-refresh", action="store_true",
                        help="show cached usage only; skip the live /api/oauth/usage fetch")
    p_add = sub.add_parser("add"); p_add.add_argument("name")
    p_add.add_argument("--email", default=None, help="pre-populate the login page email for this account")
    p_rm = sub.add_parser("remove"); p_rm.add_argument("name")
    p_link = sub.add_parser("link"); p_link.add_argument("name", nargs="?")
    p_use = sub.add_parser("use", help="compose a config dir for an account and print its export line")
    p_use.add_argument("name", nargs="?", help="account to pin (default: most-headroom account)")
    p_use.add_argument("--dir", action="store_true", help="print only the config dir path (no 'export ')")
    p_crypto = sub.add_parser("crypto", help="manage envelope encryption for the credential store")
    crypto_sub = p_crypto.add_subparsers(dest="crypto_cmd", required=True)
    crypto_sub.add_parser("status", help="show encryption status (key source + slot counts)")
    crypto_sub.add_parser("init", help="generate a master key and store it in the keyring")
    crypto_sub.add_parser("migrate", help="encrypt all plaintext store slots (idempotent)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    if args.cmd == "list":
        if not args.no_refresh:
            # refresh live usage into the ledger so `list` reflects current numbers,
            # not just whatever a prior ccrun/ccmax happened to cache
            for a in accounts.list_accounts():
                accounts.usage_for(a["name"], path="cli")
        for a in accounts.list_accounts():
            u = a.get("usage") or {}
            print(f"{a['name']:<12} {a['status']:<12} "
                  f"session={u.get('session_pct', '?')}% (resets {_fmt_reset(u.get('session_resets_at'), cfg.timezone)})  "
                  f"weekly={u.get('weekly_pct', '?')}% (resets {_fmt_reset(u.get('weekly_resets_at'), cfg.timezone)})")
        return 0
    if args.cmd == "add":
        accounts.add_account(args.name, email=args.email); return 0
    if args.cmd == "remove":
        accounts.remove_account(args.name); return 0
    if args.cmd == "link":
        names = [args.name] if args.name else [a["name"] for a in accounts.list_accounts()]
        for n in names:
            configdir.reconcile_links(n)
        return 0
    if args.cmd == "use":
        if args.name:
            known = {a["name"] for a in accounts.list_accounts()}
            if args.name not in known:
                print(f"ccswitch: unknown account {args.name!r}", file=sys.stderr)
                return 1
            name = args.name
        else:
            try:
                name = accounts.pick_account(cfg)  # most-headroom + soft reservation
            except AllAccountsBusy as busy:
                when = busy.earliest_reset.astimezone(ZoneInfo(cfg.timezone)) if busy.earliest_reset else "unknown"
                print(f"ccswitch: all accounts at usage ceiling (earliest reset: {when})", file=sys.stderr)
                return 2
        cfg_dir = configdir.compose(name)
        if args.dir:
            print(cfg_dir)
        else:
            print(f"export CLAUDE_CONFIG_DIR={shlex.quote(str(cfg_dir))}  # ccswitch: {name}")
        return 0
    if args.cmd == "crypto":
        return _cmd_crypto(args)
    return 1
