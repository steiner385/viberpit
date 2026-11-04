#!/usr/bin/env python3
"""Type text into a libvirt VM via `virsh send-key` (US layout). Usage: vmtype.py DOMAIN TEXT [--enter]."""
import subprocess
import sys

PLAIN = {c: f"KEY_{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz"}
PLAIN.update({str(d): f"KEY_{d}" for d in range(10)})
PLAIN.update({" ": "KEY_SPACE", "-": "KEY_MINUS", "=": "KEY_EQUAL", "[": "KEY_LEFTBRACE",
              "]": "KEY_RIGHTBRACE", "\\": "KEY_BACKSLASH", ";": "KEY_SEMICOLON",
              "'": "KEY_APOSTROPHE", ",": "KEY_COMMA", ".": "KEY_DOT", "/": "KEY_SLASH",
              "`": "KEY_GRAVE"})
SHIFTED = {c.upper(): f"KEY_{c.upper()}" for c in "abcdefghijklmnopqrstuvwxyz"}
SHIFTED.update(dict(zip(")!@#$%^&*(", [f"KEY_{d}" for d in range(10)])))
SHIFTED.update({"_": "KEY_MINUS", "+": "KEY_EQUAL", "{": "KEY_LEFTBRACE", "}": "KEY_RIGHTBRACE",
                "|": "KEY_BACKSLASH", ":": "KEY_SEMICOLON", '"': "KEY_APOSTROPHE",
                "<": "KEY_COMMA", ">": "KEY_DOT", "?": "KEY_SLASH", "~": "KEY_GRAVE"})


def send(dom, *keys):
    subprocess.run(["sudo", "-n", "virsh", "send-key", dom, "--holdtime", "30", *keys],
                   check=True, stdout=subprocess.DEVNULL)


def main():
    dom, text = sys.argv[1], sys.argv[2]
    for ch in text:
        if ch in PLAIN:
            send(dom, PLAIN[ch])
        elif ch in SHIFTED:
            send(dom, "KEY_LEFTSHIFT", SHIFTED[ch])
        else:
            raise SystemExit(f"untypeable: {ch!r}")
    if "--enter" in sys.argv[3:]:
        send(dom, "KEY_ENTER")


if __name__ == "__main__":
    main()
