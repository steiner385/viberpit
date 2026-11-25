"""resolve_display_decision: the one place the display mode is decided (spec §4.2, per-host §1)."""
from typing import Any

import pytest

from server.sessions.display_policy import (
    DISPLAY_AUTO, DISPLAY_NATIVE, DISPLAY_REMOTE, DISPLAY_VIRTUAL, DisplayDecision,
    resolve_display_decision,
)

_NATIVE_REASON = "desktop_locked"


def _d(**kw) -> DisplayDecision:
    base: dict[str, Any] = dict(requested=None, client_node="dobby", runner="dobby", native_available=True,
                native_unavailable_reason=_NATIVE_REASON, remote_target=None,
                remote_available=False, remote_unavailable_reason=None)
    base.update(kw)
    return resolve_display_decision(**base)


def _r(**kw) -> str:
    return _d(**kw).mode


def test_local_client_auto_is_native():
    assert _r() == DISPLAY_NATIVE
    assert _r(requested=DISPLAY_AUTO) == DISPLAY_NATIVE


def test_remote_client_is_virtual():
    assert _r(client_node="phone") == DISPLAY_VIRTUAL


def test_unknown_client_is_virtual():
    assert _r(client_node=None) == DISPLAY_VIRTUAL


def test_explicit_virtual_wins_even_when_local():
    assert _r(requested=DISPLAY_VIRTUAL) == DISPLAY_VIRTUAL


def test_explicit_native_needs_no_locality_proof():
    assert _r(requested=DISPLAY_NATIVE, client_node="phone") == DISPLAY_NATIVE


def test_native_unavailable_beats_everything():
    assert _r(native_available=False) == DISPLAY_VIRTUAL
    assert _r(requested=DISPLAY_NATIVE, native_available=False) == DISPLAY_VIRTUAL


def test_node_comparison_is_case_insensitive():
    assert _r(client_node="Dobby", runner="dobby") == DISPLAY_NATIVE


@pytest.mark.parametrize("bad", ["", "desktop", "NATIVE ", DISPLAY_REMOTE])
def test_unknown_requested_reads_as_auto(bad):
    # "remote" included: a client cannot force remote.
    assert _r(requested=bad) == DISPLAY_NATIVE
    assert _r(requested=bad, client_node="phone") == DISPLAY_VIRTUAL
    assert _r(requested=bad, client_node="phone", remote_target="host:dobby",
              remote_available=True) == DISPLAY_REMOTE


@pytest.mark.parametrize("requested,client,available,expected", [
    (None, "dobby", False, True),                 # local auto, desktop down
    (DISPLAY_NATIVE, "phone", False, True),       # explicit native, desktop down
    (DISPLAY_VIRTUAL, "dobby", False, False),     # explicit virtual is never a fallback
    (None, "phone", False, False),                # remote client was always virtual
    (None, None, False, False),
    (None, "dobby", True, False),                 # desktop up: native, no fallback
    (DISPLAY_NATIVE, "phone", True, False),
    ("bogus", "Dobby", False, True),              # unrecognised => auto; node case-insensitive
])
def test_no_target_keeps_todays_rules(requested, client, available, expected):
    d = _d(requested=requested, client_node=client, native_available=available)
    assert (d.fallback_reason == _NATIVE_REASON) is expected
    assert d.fallback_reason in (None, _NATIVE_REASON)


def test_explicit_virtual_beats_remote():
    assert _d(requested=DISPLAY_VIRTUAL, remote_target="host:dobby",
              remote_available=True) == (DISPLAY_VIRTUAL, None)


def test_explicit_native_available_beats_remote():
    assert _d(requested=DISPLAY_NATIVE, client_node="phone", remote_target="host:dobby",
              remote_available=True) == (DISPLAY_NATIVE, None)


def test_local_client_native_beats_remote():
    assert _d(remote_target="host:dobby", remote_available=True) == (DISPLAY_NATIVE, None)


def test_remote_target_available_goes_remote():
    assert _d(client_node="phone", remote_target="host:dobby",
              remote_available=True) == (DISPLAY_REMOTE, None)


def test_remote_target_unavailable_falls_back_with_reason():
    assert _d(client_node="phone", remote_target="host:dobby", remote_available=False,
              remote_unavailable_reason="bridge_unavailable") == (
                  DISPLAY_VIRTUAL, "bridge_unavailable")


def test_native_down_local_client_uses_remote_when_available():
    assert _d(native_available=False, remote_target="host:dobby",
              remote_available=True) == (DISPLAY_REMOTE, None)
