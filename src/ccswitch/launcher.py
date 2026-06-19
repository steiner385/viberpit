"""ccmax: interactive launcher + sidecar monitor (usage poll + transcript tail)."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import accounts
from .config import load_config
from .detect import detect_limit_in_jsonl_line


def _claude_cmd(cfg) -> list[str]:
    return shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))


def spawn_claude(cmd: list[str], env: dict):
    return subprocess.Popen(cmd, env=env)


def current_session_id(cfg_dir: str) -> str | None:
    """Newest *.jsonl basename (without extension) under the config dir's projects/ tree."""
    base = os.path.join(cfg_dir, "projects")
    newest, newest_mtime = None, -1.0
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".jsonl"):
                p = os.path.join(dirpath, fn)
                m = os.path.getmtime(p)
                if m > newest_mtime:
                    newest, newest_mtime = fn[:-len(".jsonl")], m
    return newest


def scan_for_limit(cfg_dir: str):
    """Scan the newest transcript for a rate-limit event. Returns ResetInfo | None."""
    sid = current_session_id(cfg_dir)
    if not sid:
        return None
    base = os.path.join(cfg_dir, "projects")
    for dirpath, _dirs, files in os.walk(base):
        if f"{sid}.jsonl" in files:
            with open(os.path.join(dirpath, f"{sid}.jsonl"), encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    info = detect_limit_in_jsonl_line(line)
                    if info is not None:
                        return info
    return None


def _poll_usage_exceeds_soft(name: str, cfg) -> bool:
    u = accounts.usage_for(name)
    return u is not None and u.session_pct >= cfg.soft_threshold * 100.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccmax", add_help=False)
    parser.add_argument("--confirm-switch", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=None)
    known, claude_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    poll = cfg.poll_interval_sec if known.poll_interval is None else known.poll_interval

    from .configdir import compose
    name = accounts.pick_account(cfg)
    resume_id: str | None = None
    while True:
        cfg_dir = str(compose(name))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg_dir)
        cmd = _claude_cmd(cfg) + (["--resume", resume_id] if resume_id else []) + claude_args
        proc = spawn_claude(cmd, env)
        switch = False
        while proc.poll() is None:
            if scan_for_limit(cfg_dir) is not None or _poll_usage_exceeds_soft(name, cfg):
                switch = True
                break
            if poll:
                time.sleep(poll)
        if not switch:
            proc.wait()
            accounts.sync_back(name)
            return 0
        resume_id = current_session_id(cfg_dir) or resume_id
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        accounts.sync_back(name)
        try:
            nxt = accounts.pick_account(cfg, exclude={name})
        except Exception as e:
            print(f"ccmax: cannot switch ({e})", file=sys.stderr)
            return 2
        if known.confirm_switch:
            print(f"ccmax: {name} hit its limit. Switch to {nxt} and resume? [Y/n] ", end="")
            if input().strip().lower() in ("n", "no"):
                return 0
        print(f"ccmax: {name} hit its limit — switching to {nxt} and resuming…", file=sys.stderr)
        name = nxt
