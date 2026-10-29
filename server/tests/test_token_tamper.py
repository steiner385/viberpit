"""The shared tamper helper must defeat the base64 padding-bit collision that made
``tok[:-2] + "zz"`` a flaky tamper (#3180)."""
from __future__ import annotations

import pytest
from itsdangerous import BadSignature, URLSafeSerializer

from server.tests._token_tamper import tamper_signature


def _colliding_token(s: URLSafeSerializer) -> str:
    # ~1 payload in 1024 signs to a signature ending "z" + [wxyz]; scan for one.
    for i in range(100_000):
        tok = s.dumps(i)
        if tok[-2] == "z" and tok[-1] in "wxyz":
            return tok
    raise AssertionError("no colliding signature found")


def test_tail_overwrite_is_not_a_tamper_but_helper_is():
    s = URLSafeSerializer("x" * 32, salt="tamper-test")
    tok = _colliding_token(s)
    # The premise: the old tail overwrite still verifies on this token.
    assert s.loads(tok[:-2] + "zz") == s.loads(tok)
    with pytest.raises(BadSignature):
        s.loads(tamper_signature(tok))


def test_helper_always_changes_the_signature():
    s = URLSafeSerializer("x" * 32, salt="tamper-test")
    for i in range(2000):
        tok = s.dumps(i)
        with pytest.raises(BadSignature):
            s.loads(tamper_signature(tok))
