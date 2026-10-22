"""Fixture identities for the trusted-author gate (#3147).

A user is trusted by their immutable GitHub id, never their login: a policy lists
``login#<id>`` and a mirrored row carries ``WorkItem.author_id``. Tests that need a
trusted human seed ``author_id=TONY_ID`` (or ``STEINER_ID``) on the row and the matching
``*_ENTRY`` in ``trusted_authors``; ``*_TRUST`` is the same user as the queue-shaped set
``product_policy.trusted_authors`` returns.
"""
from __future__ import annotations

TONY_ID = 1234567
STEINER_ID = 385385

TONY_ENTRY = f"tony#{TONY_ID}"
STEINER_ENTRY = f"steiner385#{STEINER_ID}"

TONY_TRUST = frozenset({f"user:{TONY_ID}"})
STEINER_TRUST = frozenset({f"user:{STEINER_ID}"})
