#!/usr/bin/env python3
"""Fake interactive claude process for PTY-manager testing.

Env vars:
  CCSWITCH_FAKE_IDLE_AFTER  seconds of no-input before going silent (default 0 = never idle)
  CCSWITCH_FAKE_EXIT_ON     input line that causes exit 0 (default "/quit")
"""
import os
import sys
import time

# Force completely unbuffered stdout for PTY operation
sys.stdout = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)  # type: ignore[assignment]
sys.stdin = os.fdopen(sys.stdin.fileno(), "rb", buffering=0)   # type: ignore[assignment]


def write(line: str) -> None:
    sys.stdout.write((line + "\r\n").encode())  # type: ignore[attr-defined]


def main() -> None:
    idle_after = float(os.environ.get("CCSWITCH_FAKE_IDLE_AFTER", "0") or "0")
    exit_on = os.environ.get("CCSWITCH_FAKE_EXIT_ON", "/quit")

    write("fake-claude ready")

    turn = 0
    buf = b""
    while True:
        # Respect idle: only read for idle_after seconds if configured
        if idle_after > 0:
            import select
            ready, _, _ = select.select([sys.stdin], [], [], idle_after)
            if not ready:
                # idle — do nothing (let caller detect idle)
                time.sleep(0.05)
                continue
            chunk = sys.stdin.read(256)  # type: ignore[attr-defined]
        else:
            chunk = sys.stdin.read(256)  # type: ignore[attr-defined]

        if not chunk:
            break

        buf += chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.rstrip(b"\r").decode(errors="replace")
            if line == exit_on:
                sys.exit(0)
            turn += 1
            write(f"<<turn{turn}>> got: {line}")
            write(f"<<turn{turn}-done>>")


if __name__ == "__main__":
    main()
