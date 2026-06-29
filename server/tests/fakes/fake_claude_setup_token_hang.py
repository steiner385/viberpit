#!/usr/bin/env python3
"""Fake claude binary simulating an invalid-code / non-completing flow.

Timeline:
1. Sets stdin to raw mode (like real claude TUI).
2. Prints the OAuth authorize URL.
3. Waits for the code (\\r-terminated, up to 10 s).
4. Prints the "OAuth error: Invalid code…" message (matching real claude output).
5. Loops forever — never exits.

Used to verify that post_code_timeout_s kills the hung child promptly and
capture_setup_token returns False without hanging.
"""
import os
import select
import sys
import termios
import time
import tty

fd = sys.stdin.fileno()
try:
    tty.setraw(fd)
except termios.error:
    pass

sys.stdout.write(
    "Browser didn't open? Use the url below:\n"
    "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=fakestate\n"
)
sys.stdout.flush()

# Wait for \\r (code submission) up to 10 s
buf = b""
while True:
    rlist, _, _ = select.select([fd], [], [], 10.0)
    if not rlist:
        sys.exit(1)  # no code arrived
    chunk = os.read(fd, 256)
    if not chunk:
        sys.exit(1)
    buf += chunk
    if b"\r" in buf:
        break

# Print the "invalid code" reprompt (matches real claude TUI output)
sys.stdout.write(
    "OAuth error: Invalid code. Please make sure the full code was copied."
    " Press Enter to retry.\n"
)
sys.stdout.flush()

# Hang forever — simulating a re-prompt that never completes
while True:
    time.sleep(10)
