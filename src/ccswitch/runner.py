"""ccrun: headless wrapper. Selects the best account, runs `claude -p`, retries on limit."""
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

from . import accounts
from .config import load_config
from .configdir import compose
from .crypto import read_oauth_token
from .detect import detect_limit_in_result
from .selector import AllAccountsBusy


def _claude_cmd(cfg) -> list[str]:
    raw = os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
    return shlex.split(raw)


def run_claude(cmd: list[str], env: dict) -> tuple[int, str]:
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout


def _last_result(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            return obj
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    wait = False
    if argv and argv[0] == "--wait":
        wait = True
        argv = argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    cfg = load_config()
    tried: set[str] = set()
    seen_resets: list[datetime] = []
    while True:
        try:
            name = accounts.pick_account(cfg, exclude=tried)
        except AllAccountsBusy as busy:
            if wait and busy.earliest_reset is not None:
                delay = max(1.0, (busy.earliest_reset - datetime.now(busy.earliest_reset.tzinfo)).total_seconds())
                time.sleep(delay)
                tried.clear()
                continue
            when = busy.earliest_reset.astimezone() if busy.earliest_reset else "unknown"
            print(f"ccrun: all accounts at usage ceiling (earliest reset: {when})", file=sys.stderr)
            return 2
        cfg_dir = compose(name)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg_dir))
        tok = read_oauth_token(name)
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        rc, stdout = run_claude(_claude_cmd(cfg) + argv, env)
        sys.stdout.write(stdout)
        result = _last_result(stdout)
        limit = detect_limit_in_result(result) if result else None
        if limit is None:
            accounts.sync_back(name)
            return rc
        tried.add(name)
        if limit.resets_at is not None:
            seen_resets.append(limit.resets_at)
        print(f"ccrun: {name} hit its usage limit — rotating to another account", file=sys.stderr)
        if len(tried) >= len(accounts.list_accounts()):
            earliest = min(seen_resets) if seen_resets else None
            if wait:
                if earliest is not None:
                    delay = max(1.0, (earliest - datetime.now(earliest.tzinfo)).total_seconds())
                    time.sleep(delay)
                else:
                    time.sleep(5)
                seen_resets.clear()
                tried.clear()
                continue
            when = earliest.astimezone() if earliest is not None else "unknown"
            print(f"ccrun: all accounts exhausted (earliest reset: {when})", file=sys.stderr)
            return 2
