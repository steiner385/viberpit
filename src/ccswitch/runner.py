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
from .detect import detect_limit_in_result
from .selector import AllAccountsBusy


def _claude_cmd(cfg) -> list[str]:
    raw = os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
    return shlex.split(raw)


def run_claude(cmd: list[str], env: dict) -> tuple[int, str]:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
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
    from .configdir import compose
    tried: set[str] = set()
    n_accounts = len(accounts.list_accounts())
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
        rc, stdout = run_claude(_claude_cmd(cfg) + argv, env)
        sys.stdout.write(stdout)
        result = _last_result(stdout)
        limit = detect_limit_in_result(result) if result else None
        if limit is None:
            accounts.sync_back(name)
            return rc
        tried.add(name)
        print(f"ccrun: {name} hit its usage limit — rotating to another account", file=sys.stderr)
        if len(tried) >= max(1, n_accounts):
            if wait:
                tried.clear()
                time.sleep(5)
                continue
            print("ccrun: all accounts exhausted", file=sys.stderr)
            return 2
