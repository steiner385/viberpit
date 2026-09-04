"""ccswitch: account store management (add / remove / list / link / use / crypto)
plus clone maintenance (clones doctor / repair / remove)."""
import argparse
import shlex
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import accounts, configdir
from .config import load_config
from .selector import AllAccountsBusy


def _fmt_reset(iso: str | None, tz: str) -> str:
    # No active window (resets_at is None => 0% used) reads as a clear "fresh"
    # state rather than a bare dash, per issue #16 acceptance criteria.
    if not iso:
        return "fresh"
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
            for slot in accounts.anthropic_slot_paths():
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
        # Enumerate via the shared helper, not a second `*.cred.json` glob: a stray
        # dotfile slot would otherwise be "migrated" here and written back, resurrecting
        # the phantom account this helper exists to filter out.
        for slot in accounts.anthropic_slot_paths():
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


def _repo_root() -> str:
    """The repo root whose ``.ccswitch/`` namespace the clone commands act on.

    Mirrors the server's ``CCSWITCH_REPO_ROOT`` setting (server/config.py) so the
    CLI operates on exactly the clones and worktrees the daemon does — the CLI is
    the recovery tool for a daemon-owned tree, not a separate namespace."""
    import os

    return os.environ.get("CCSWITCH_REPO_ROOT", os.path.expanduser("~/GitHub"))


def _cmd_clones(args: argparse.Namespace) -> int:
    """Handle the `ccswitch clones` subcommand family (issue #1003)."""
    import json

    from . import clone_doctor

    repo_root = _repo_root()
    sub = args.clones_cmd

    if sub == "doctor":
        reports = clone_doctor.diagnose(repo_root)
        if getattr(args, "json", False):
            print(json.dumps([
                {
                    "clone": r.clone_dir,
                    "repo": r.name_with_owner,
                    "cloneExists": r.exists,
                    "worktrees": len(r.links),
                    "broken": [link.path for link in r.broken],
                }
                for r in reports
            ], indent=2))
            return 0
        if not reports:
            print(f"ccswitch clones doctor: no session worktrees under {repo_root}")
            return 0
        broken_total = 0
        for r in reports:
            broken_total += len(r.broken)
            state = "ok" if r.healthy else ("MISSING" if not r.exists else "BROKEN LINKS")
            print(f"{r.name_with_owner or r.clone_dir}: clone {state} at {r.clone_dir} — "
                  f"{len(r.links)} worktree(s), {len(r.broken)} broken")
        if broken_total:
            print(f"\n{broken_total} orphaned worktree(s). Working files are intact; "
                  "recover with: ccswitch clones repair")
            return 1
        return 0

    if sub == "repair":
        results = clone_doctor.repair(repo_root, dry_run=args.dry_run, base_ref=args.base_ref)
        if not results:
            print("ccswitch clones repair: nothing to repair")
            return 0
        failed = 0
        for entry in results:
            outcomes = entry.get("results") or {}
            errors = [f"{p}: {o}" for p, o in outcomes.items() if o.startswith("error")]
            failed += len(errors) + (1 if entry["action"] == "failed" else 0)
            print(f"{entry['repo'] or entry['clone']}: {entry['action']} "
                  f"({len(entry['worktrees'])} worktree(s))")
            if entry.get("error"):
                print(f"  error: {entry['error']}")
            for line in errors:
                print(f"  {line}")
        if not args.dry_run:
            print("\nRecovered worktrees are DETACHED at the remote default tip "
                  "(the branch died with the clone); uncommitted work is untouched.")
        return 1 if failed else 0

    if sub == "remove":
        # Resolve through gitrepos.clone_path (not a hand-built path) so a
        # case-variant repo name names the clone ensure_clone would use (#1003).
        from .gitrepos import clone_path

        try:
            result = clone_doctor.remove_clone(
                repo_root,
                clone_path(repo_root, args.repo),
                reason=args.reason,
                force=args.force,
            )
        except ValueError as exc:
            print(f"ccswitch clones remove: {exc}", file=sys.stderr)
            return 1
        except clone_doctor.CloneInUseError as exc:
            print(f"ccswitch clones remove: {exc}", file=sys.stderr)
            print("Pass --force only if you accept killing those sessions' git metadata.",
                  file=sys.stderr)
            return 2
        if result["removed"]:
            print(f"removed clone {result['repo']} at {result['clone']}")
        else:
            print(f"clone {result['repo']} is not present at {result['clone']}")
        return 0

    print(f"ccswitch clones: unknown sub-command {sub!r}", file=sys.stderr)
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
    p_clones = sub.add_parser(
        "clones", help="inspect, repair, or safely remove ccswitch-owned repo clones")
    clones_sub = p_clones.add_subparsers(dest="clones_cmd", required=True)
    p_doctor = clones_sub.add_parser(
        "doctor", help="report session worktrees whose shared clone is missing or unlinked")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable output")
    p_repair = clones_sub.add_parser(
        "repair", help="re-clone missing clones and re-link their orphaned worktrees")
    p_repair.add_argument("--dry-run", action="store_true",
                          help="report what would be repaired; change nothing")
    p_repair.add_argument("--base-ref", default=None,
                          help="ref to detach recovered worktrees at (default: origin/HEAD)")
    p_remove = clones_sub.add_parser(
        "remove", help="remove a clone, refusing while session worktrees still use it")
    p_remove.add_argument("repo", help="nameWithOwner, e.g. cairnea/KinDash")
    p_remove.add_argument("--reason", default="operator request",
                          help="recorded in the audit log line")
    p_remove.add_argument("--force", action="store_true",
                          help="remove even though worktrees reference it (destructive)")
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
    if args.cmd == "clones":
        return _cmd_clones(args)
    return 1
