#!/usr/bin/env python3
"""Fake claude binary for testing capture_setup_token.

When invoked as `claude setup-token`, it:
1. Prints a URL line (as the real claude Ink TUI would)
2. Reads one line from stdin (the auth code)
3. Prints the success message with a fake sk-ant-oat01 token
4. Exits 0.

No real claude binary, no network, no PTY colour codes.
"""
import sys

# Print the OAuth authorize URL (the real binary does this via Ink TUI)
print(
    "Browser didn't open? Use the url below:\n"
    "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=fakestate",
    flush=True,
)

# Read the auth code from stdin (the user/caller pastes it)
_code = sys.stdin.readline().strip()

# Print success + token (real binary prints this after code submission)
print("✓ Long-lived authentication token created successfully!")
print("sk-ant-oat01-FAKETOKEN0123456789")
sys.exit(0)
