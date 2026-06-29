#!/usr/bin/env python3
"""Fake claude binary for testing capture_setup_token.

Mimics real claude's raw-mode TUI behavior:
- Calls tty.setraw() on stdin BEFORE printing anything, so ICRNL is
  disabled and \\r is NOT silently converted to \\n by the PTY line
  discipline.  (This is exactly what the real Ink/claude TUI does.)
- Treats \\r (carriage-return) as Enter/submit; ignores bare \\n.
- If no \\r arrives within 3 s, exits(1) without printing the token.
  (Simulates "code never submitted" — the real claude TUI would hang,
  but a clean exit keeps the test deterministic and avoids hangs.)

This means: only the carriage-return fix (\\n → \\r in capture_setup_token)
makes the existing happy-path test pass.
"""
import os
import select
import sys
import termios
import tty

# Switch stdin to raw mode BEFORE printing the URL — same as real claude TUI.
# Disabling ICRNL means \\r on the PTY master is received as \\r here (not
# translated to \\n by the kernel's line discipline).  Calling setraw first
# also eliminates any race with the caller writing the code before setraw runs.
fd = sys.stdin.fileno()
try:
    tty.setraw(fd)
except termios.error:
    pass  # not a real TTY in some environments — proceed anyway

# Print the OAuth authorize URL (the real binary does this via Ink TUI).
# Output is in raw mode (OPOST disabled → no ONLCR \\n→\\r\\n translation),
# but the URL regex in capture_setup_token doesn't rely on \\r\\n.
sys.stdout.write(
    "Browser didn't open? Use the url below:\n"
    "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=fakestate\n"
)
sys.stdout.flush()

# Read raw bytes until \\r arrives or the 3-second read timeout fires.
_READ_TIMEOUT = 3.0
buf = b""
while True:
    rlist, _, _ = select.select([fd], [], [], _READ_TIMEOUT)
    if not rlist:
        # No \\r arrived within the timeout — "code never submitted"
        sys.exit(1)
    chunk = os.read(fd, 256)
    if not chunk:
        sys.exit(1)
    buf += chunk
    if b"\r" in buf:
        break  # carriage-return received — proceed

# Print success + token (real binary prints this after code submission)
sys.stdout.write("✓ Long-lived authentication token created successfully!\n")
sys.stdout.write("sk-ant-oat01-FAKETOKEN0123456789\n")
sys.stdout.flush()
sys.exit(0)
