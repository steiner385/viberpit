"""ccswitch: account store management (add / remove / list / link / use)."""
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
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    if args.cmd == "list":
        if not args.no_refresh:
            # refresh live usage into the ledger so `list` reflects current numbers,
            # not just whatever a prior ccrun/ccmax happened to cache
            for a in accounts.list_accounts():
                accounts.usage_for(a["name"])
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
    return 1
