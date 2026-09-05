"""``ledger.json`` is CROSS-HOST, so a v2 blob must stay readable by an OLD host.

The fleet upgrades host by host (rocky is routinely left a deploy behind), and
``locked_ledger``'s own docstring says every host writes this file. The moment
the primary's poller rewrote every account's blob as v2-only, the lagging host's
pre-v2 reader — ``Usage(blob["session_pct"], …)``, no guard, called from
``accounts.usage_for`` inside ``locked_ledger`` with no ``try`` — would raise
``KeyError`` for EVERY account. Its usage cache would go all-``None`` and
``select_picked(optimistic_fallback=False)`` would raise
``AllAccountsBusy(kind="no_usable_accounts")``: no session could spawn there.

Both directions are pinned here: old-reader-on-new-blob and new-reader-on-old-blob.
The old reader is a literal, self-contained copy of the pre-change parsing so
these tests keep working after the real old code is gone.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ccswitch import ledger
from ccswitch.quota import (QUOTA_BLOB_VERSION, anthropic_snapshot,
                            copilot_snapshot)
from tests.helpers import temp_env

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _legacy_cached_usage(blob):
    """A literal copy of the PRE-CHANGE ``ledger.cached_usage`` parsing.

    Copied rather than imported on purpose: the point of the test is that a host
    running code this repo no longer contains can still read what we write, so it
    must not track our refactors. Returns the five legacy fields as a tuple in
    place of the deleted ``oauth.Usage``.
    """
    sr = blob.get("session_resets_at")
    wr = blob.get("weekly_resets_at")
    return (
        blob["session_pct"], datetime.fromisoformat(sr) if sr else None,
        blob["weekly_pct"], datetime.fromisoformat(wr) if wr else None,
        blob["extra_usage_enabled"],
    )


def _record(name, snapshot):
    with temp_env():
        with ledger.locked_ledger() as led:
            ledger.record_usage(led, name, snapshot, NOW)
            return led["accounts"][name]["usage"]


def test_new_blob_is_still_versioned_v2():
    """The dual-write must not disturb the canonical encoding."""
    blob = _record("work", anthropic_snapshot(11.0, NOW, 69.0, NOW))
    assert blob["v"] == QUOTA_BLOB_VERSION
    assert [w["key"] for w in blob["windows"]] == ["session", "weekly"]


def test_old_reader_can_parse_a_new_anthropic_blob():
    """The failure this exists to stop: KeyError('session_pct') on every account."""
    s_reset = NOW + timedelta(hours=3)
    w_reset = NOW + timedelta(days=2)
    blob = _record("work", anthropic_snapshot(11.0, s_reset, 69.0, w_reset, True))
    assert _legacy_cached_usage(blob) == (11.0, s_reset, 69.0, w_reset, True)


def test_old_reader_sees_a_copilot_blob_exactly_as_it_did_before():
    """Copilot now has ONE window; the legacy emit must reproduce the old
    projection (the same pct and reset on BOTH dimensions) so a pre-upgrade
    host's selector ranks the account identically to before the refactor."""
    reset = NOW + timedelta(days=10)
    blob = _record("cp", copilot_snapshot(37.5, reset))
    assert _legacy_cached_usage(blob) == (37.5, reset, 37.5, reset, False)


def test_old_reader_handles_a_window_with_no_reset():
    """``resets_at=None`` is a real state (a fully fresh account), not an error."""
    blob = _record("fresh", anthropic_snapshot(0.0, None, 0.0, None))
    assert _legacy_cached_usage(blob) == (0.0, None, 0.0, None, False)


def test_legacy_fields_are_derived_not_a_second_computation():
    """No parallel path: every legacy value is projected out of the windows the
    snapshot already carries."""
    u = anthropic_snapshot(11.0, NOW, 69.0, NOW, True)
    legacy = ledger._legacy_usage_fields(u)
    assert legacy["session_pct"] == u.window("session").used_pct
    assert legacy["weekly_pct"] == u.window("weekly").used_pct
    assert legacy["extra_usage_enabled"] is u.extra_usage_enabled


@pytest.mark.parametrize("snapshot", [
    anthropic_snapshot(11.0, NOW, 69.0, NOW, True),
    copilot_snapshot(37.5, NOW),
])
def test_new_reader_ignores_the_legacy_keys(snapshot):
    """``usage_from_blob`` keys off ``v``, so the compat emit is invisible to us."""
    with temp_env():
        with ledger.locked_ledger() as led:
            ledger.record_usage(led, "acct", snapshot, NOW)
            blob = led["accounts"]["acct"]["usage"]
        assert ledger.usage_from_blob("acct", blob).windows == snapshot.windows


def test_new_reader_still_migrates_an_old_blob():
    """The other direction: a blob written by a lagging host has no ``v``."""
    reset = NOW + timedelta(hours=3)
    old_blob = {
        "session_pct": 11.0, "session_resets_at": reset.isoformat(),
        "weekly_pct": 69.0, "weekly_resets_at": reset.isoformat(),
        "extra_usage_enabled": True, "fetched_at": NOW.isoformat(),
    }
    with temp_env():
        u = ledger.usage_from_blob("work", old_blob)
    assert [(w.key, w.used_pct) for w in u.windows] == [("session", 11.0), ("weekly", 69.0)]
    assert u.extra_usage_enabled is True
    assert u.is_available


def test_round_trip_through_the_old_encoding_is_lossless_for_the_selector():
    """Write v2 -> read with the OLD parser -> re-encode as the OLD writer would
    -> read with the NEW parser. The selector's inputs survive the full
    upgrade/downgrade cycle, which is what a partially-deployed fleet does."""
    reset = NOW + timedelta(hours=3)
    blob = _record("work", anthropic_snapshot(11.0, reset, 69.0, reset, True))
    s_pct, s_at, w_pct, w_at, extra = _legacy_cached_usage(blob)
    rewritten = {
        "session_pct": s_pct, "session_resets_at": s_at.isoformat(),
        "weekly_pct": w_pct, "weekly_resets_at": w_at.isoformat(),
        "extra_usage_enabled": extra, "fetched_at": NOW.isoformat(),
    }
    with temp_env():
        u = ledger.usage_from_blob("work", rewritten)
    assert [(w.key, w.used_pct, w.resets_at) for w in u.windows] == [
        ("session", 11.0, reset), ("weekly", 69.0, reset)]
