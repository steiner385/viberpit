"""Deterministic signature tampering for itsdangerous-signed bearer tokens.

Overwriting a token's tail (``tok[:-2] + "zz"``) is NOT a guaranteed tamper: the
20-byte HMAC encodes to 27 base64 chars, so the last char carries 2 padding bits.
A signature that already ends ``z`` + one of ``wxyz`` decodes to the very same
digest after the "tamper" and still verifies (~1 run in 1024, redrawn every second
by the embedded timestamp — #3180). Flipping the signature's FIRST char changes
six fully significant bits, so the result can never verify.
"""
from __future__ import annotations


def tamper_signature(token: str) -> str:
    head, sep, sig = token.rpartition(".")
    assert sep and sig, f"not a signed token: {token!r}"
    flipped = "A" if sig[0] != "A" else "B"
    return head + sep + flipped + sig[1:]
