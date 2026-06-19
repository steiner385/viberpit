"""ccswitch: account store management (add / remove / list / link)."""
import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import accounts, configdir
from .config import load_config


def _fmt_reset(iso: str | None, tz: str) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%a %-I:%M %p %Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccswitch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_add = sub.add_parser("add"); p_add.add_argument("name")
    p_rm = sub.add_parser("remove"); p_rm.add_argument("name")
    p_link = sub.add_parser("link"); p_link.add_argument("name", nargs="?")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    if args.cmd == "list":
        for a in accounts.list_accounts():
            u = a.get("usage") or {}
            print(f"{a['name']:<12} {a['status']:<12} "
                  f"session={u.get('session_pct', '?')}% (resets {_fmt_reset(u.get('session_resets_at'), cfg.timezone)})  "
                  f"weekly={u.get('weekly_pct', '?')}% (resets {_fmt_reset(u.get('weekly_resets_at'), cfg.timezone)})")
        return 0
    if args.cmd == "add":
        accounts.add_account(args.name); return 0
    if args.cmd == "remove":
        accounts.remove_account(args.name); return 0
    if args.cmd == "link":
        names = [args.name] if args.name else [a["name"] for a in accounts.list_accounts()]
        for n in names:
            configdir.reconcile_links(n)
        return 0
    return 1
