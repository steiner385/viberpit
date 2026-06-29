#!/usr/bin/env python3
"""Fake claude binary for testing capture_setup_token failure path.

Prints the authorize URL, reads a code, then prints an error message
with NO token — simulates a bad code / rejected auth.
"""
import sys

print(
    "Browser didn't open? Use the url below:\n"
    "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=fakestate",
    flush=True,
)

_code = sys.stdin.readline().strip()

print("Error: invalid authorization code")
sys.exit(1)
