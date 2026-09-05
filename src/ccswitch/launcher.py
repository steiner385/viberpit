"""ccmax: interactive launcher + sidecar monitor (usage poll + transcript tail)."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from . import accounts
from .config import load_config
from .crypto import read_oauth_token
from .detect import detect_limit_in_jsonl_line
from .selector import at_ceiling, binding_window, usage_known


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


def _proactive_target(name, cfg):
    """Return a strictly-less-loaded usable account to pre-empt to, or None.

    "Loaded" is measured on the BINDING window (whichever allowance the account
    runs out of first), so this reads the same number the selector ranks on
    instead of assuming a dimension named "session". "Usable" is likewise the
    shared ``selector.at_ceiling`` definition — ANY window at/above the ceiling
    disqualifies a candidate, so a weekly-exhausted account is never offered as a
    pre-emption target.
    """
    active = accounts.usage_for(name)
    active_pct = _binding_pct(active)
    if active_pct is None or active_pct < cfg.soft_threshold * 100.0:
        return None
    ceiling = cfg.hard_ceiling * 100.0
    best, best_pct = None, active_pct
    for other in [a["name"] for a in accounts.list_accounts() if a["name"] != name]:
        u = accounts.usage_for(other)
        pct = _binding_pct(u)
        if pct is None:
            continue
        if at_ceiling(u, ceiling):
            continue
        if pct < best_pct:
            best, best_pct = other, pct
    return best


def _binding_pct(u):
    """Used-percent of the account's binding window, or None when usage is unknown."""
    if not usage_known(u):
        return None
    w = binding_window(u)
    return None if w is None else w.used_pct


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccmax", add_help=False)
    parser.add_argument("--confirm-switch", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=None)
    known, claude_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    poll = cfg.poll_interval_sec if known.poll_interval is None else known.poll_interval

    from .configdir import compose

    def _poll_loop(proc, cfg_dir):
        """Block until proc exits or a switch is warranted. Returns "hard"/"proactive"/None."""
        while proc.poll() is None:
            if scan_for_limit(cfg_dir) is not None:
                return "hard"
            if _proactive_target(name, cfg) is not None:
                return "proactive"
            if poll:
                time.sleep(poll)
        return None

    name = accounts.pick_account(cfg)
    resume_id: str | None = None
    while True:
        cfg_dir = str(compose(name))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg_dir)
        tok = read_oauth_token(name)
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        cmd = _claude_cmd(cfg) + (["--resume", resume_id] if resume_id else []) + claude_args
        proc = spawn_claude(cmd, env)
        # Inner loop: poll the SAME live process. Only re-spawn (outer loop) after an actual
        # switch. A proactive trigger that can't secure a replacement (or a declined switch)
        # re-enters this inner loop on the existing proc — never killing a working session.
        switched = False
        while not switched:
            switch_reason = _poll_loop(proc, cfg_dir)
            if switch_reason is None:
                proc.wait()
                accounts.sync_back(name)
                return 0
            # secure the next account BEFORE killing, so a proactive switch never strands us
            try:
                nxt = accounts.pick_account(cfg, exclude={name})
            except Exception as e:
                if switch_reason == "proactive":
                    # no better account actually available (race) — keep running on current
                    continue
                # hard limit: current account is unusable; report earliest reset and stop
                print(f"ccmax: {name} hit its limit and no other account is available ({e})", file=sys.stderr)
                resume_id = current_session_id(cfg_dir) or resume_id
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                accounts.sync_back(name)
                return 2
            if known.confirm_switch:
                print(f"ccmax: {name} is busy. Switch to {nxt} and resume? [Y/n] ", end="")
                if input().strip().lower() in ("n", "no"):
                    # user declined; we have NOT killed the process — keep running on current
                    continue
            resume_id = current_session_id(cfg_dir) or resume_id  # newest transcript = the active session (assumes it is the most-recently-modified *.jsonl)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            accounts.sync_back(name)
            print(f"ccmax: switching from {name} to {nxt} and resuming…", file=sys.stderr)
            name = nxt
            switched = True
