"""resolve_display_mode: the one place 'native vs virtual' is decided (spec §4.2)."""
import pytest

from server.sessions.display_policy import (
    DISPLAY_AUTO, DISPLAY_NATIVE, DISPLAY_VIRTUAL, resolve_display_mode,
)


def _r(**kw):
    base = dict(requested=None, client_node="dobby", runner="dobby", native_available=True)
    base.update(kw)
    return resolve_display_mode(**base)


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


@pytest.mark.parametrize("bad", ["", "desktop", "NATIVE "])
def test_unrecognised_request_is_treated_as_auto(bad):
    assert _r(requested=bad) == DISPLAY_NATIVE
    assert _r(requested=bad, client_node="phone") == DISPLAY_VIRTUAL


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
def test_native_fallback_truth_table(requested, client, available, expected):
    from server.sessions.display_policy import native_fallback
    assert native_fallback(requested=requested, client_node=client, runner="dobby",
                           native_available=available) is expected
