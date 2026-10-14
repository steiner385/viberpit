"""Opt-in trust of the edge host for human-only writes (spec 2026-10-14)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.config import get_settings


def test_rules_trust_local_host_defaults_off():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIBERPIT_RULES_TRUST_LOCAL_HOST", None)
        os.environ.pop("CCSWITCH_RULES_TRUST_LOCAL_HOST", None)
        assert get_settings().rules_trust_local_host is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("0", False),
])
def test_rules_trust_local_host_parses_like_sibling_flags(raw, expected):
    with patch.dict(os.environ, {"VIBERPIT_RULES_TRUST_LOCAL_HOST": raw,
                                 "VIBERPIT_PERMISSION_RULES_ENABLED": raw}):
        s = get_settings()
        assert s.rules_trust_local_host is expected
        assert s.rules_trust_local_host == s.permission_rules_enabled


def test_rules_trust_local_host_is_listed_on_about():
    from server.api.settings import _MANAGED
    row = next(r for r in _MANAGED if r[0] == "rules_trust_local_host")
    assert row[1] == "VIBERPIT_RULES_TRUST_LOCAL_HOST"
    assert "agents on the host" in row[2]
