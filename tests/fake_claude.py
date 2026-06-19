#!/usr/bin/env python3
"""Fake `claude`. CCSWITCH_FAKE_MODE drives behavior; writes JSON to stdout.

Modes (per CLAUDE_CONFIG_DIR basename == account name):
  CCSWITCH_FAKE_LIMITED=alpha,beta  -> those accounts emit a 429 result, others succeed.
"""
import json
import os
import sys


def main():
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    account = os.path.basename(cfg_dir.rstrip("/"))
    limited = set(filter(None, os.environ.get("CCSWITCH_FAKE_LIMITED", "").split(",")))
    if account in limited:
        print(json.dumps({"type": "result", "is_error": True,
                          "error": {"message": "Claude usage limit reached", "resets_at": "2026-06-19T18:00:00+00:00"}}))
        return 1
    print(json.dumps({"type": "result", "is_error": False, "result": f"ok from {account}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
