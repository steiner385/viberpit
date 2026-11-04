from server.mcp_health import overall as o

NOW = 1_800_000_000_000


def b(name="aws", url="https://u?x=1", tok="t", exp=NOW + 3_600_000, rt="r"):
    return {"serverName": name, "serverUrl": url, "accessToken": tok, "expiresAt": exp, "refreshToken": rt}


def test_grant_none_without_bundle():
    assert o.grant_state({}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "none"


def test_grant_never_for_cleared_bundle():
    assert o.grant_state({"k": b(tok="", exp=0, rt=None)}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "never"


def test_grant_live_expiring_expired():
    def s(exp):
        return {"k": b(exp=exp)}

    assert o.grant_state(s(NOW + 3_600_000), key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"
    assert o.grant_state(s(NOW + 60_000), key_name="aws", url="https://u?x=1", now_ms=NOW) == "expiring"
    assert o.grant_state(s(NOW - 1), key_name="aws", url="https://u?x=1", now_ms=NOW) == "expired"


def test_grant_ignores_orphan_bundle_with_other_url():
    store = {"orphan": b(url="https://u", tok="", exp=0, rt=None), "live": b()}
    assert o.grant_state(store, key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"
    assert o.grant_state({"orphan": b(url="https://u")}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "none"


def test_grant_best_bundle_wins():
    store = {"a": b(tok="", exp=0, rt=None), "b": b()}
    assert o.grant_state(store, key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"


def test_best_expiry():
    store = {"a": b(exp=NOW - 5), "b": b(exp=NOW - 1), "c": b(tok="", exp=NOW + 9)}
    assert o.best_expiry(store, key_name="aws", url=None) == NOW - 1
    assert o.best_expiry({}, key_name="aws", url=None) is None


def _iso(age_s: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW / 1000 - age_s, tz=timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _fresh() -> str:
    return _iso(60)


def test_disabled_account_row_not_counted():
    row = {"account": "old", "source": "session", "sessionId": "s1", "status": "needs-auth",
           "observedAt": _fresh()}
    assert not o.counted(row, enabled_accounts={"a"}, live_session_ids={"s1"}, now_ms=NOW)


def test_stale_row_not_counted_recent_row_counted_missing_not_counted():
    base = {"account": "a", "source": "probe", "sessionId": None, "status": "connected"}
    kw = dict(enabled_accounts={"a"}, live_session_ids=set(), now_ms=NOW)
    assert not o.counted({**base, "observedAt": _iso(25 * 3600)}, **kw)
    assert o.counted({**base, "observedAt": _iso(3600)}, **kw)
    assert not o.counted(base, **kw)
    assert not o.counted({**base, "observedAt": "garbage"}, **kw)


def test_ended_session_row_not_counted_probe_row_counted():
    sess = {"account": "a", "source": "session", "sessionId": "gone", "status": "connected",
            "observedAt": _fresh()}
    probe = {"account": "a", "source": "probe", "sessionId": None, "status": "connected",
             "observedAt": _fresh()}
    assert not o.counted(sess, enabled_accounts={"a"}, live_session_ids=set(), now_ms=NOW)
    assert o.counted(probe, enabled_accounts={"a"}, live_session_ids=set(), now_ms=NOW)


def test_never_reads_needs_auth():
    out = o.derive_overall(grant="never", rows=[{"status": "connected", "error": None, "source": "probe"}], now_ms=NOW, expires_at_ms=None)
    assert out == {"status": "needs-auth", "error": "Not signed in — one sign-in covers every account and runner.", "grant": "never"}


def test_expired_copy_names_utc_expiry():
    out = o.derive_overall(grant="expired", rows=[], now_ms=NOW, expires_at_ms=1_799_996_400_000)
    assert out["status"] == "needs-auth"
    assert out["error"] == "Sign-in expired 2027-01-15 07:00 UTC — background refresh is failing; sign in again."


def test_config_failure_beats_missing_grant():
    rows = [{"status": "failed", "error": "Incompatible auth server: does not support dynamic client registration", "source": "probe"},
            {"status": "needs-auth", "error": "Not signed in", "source": "probe"}]
    out = o.derive_overall(grant="never", rows=rows, now_ms=NOW, expires_at_ms=None)
    assert out["status"] == "failed" and "dynamic client registration" in out["error"]


def test_probe_needs_auth_ignored_when_grant_live():
    rows = [{"status": "needs-auth", "error": "Sign-in expired 2026-10-18 03:11 UTC — re-authenticate.", "source": "probe"},
            {"status": "connected", "error": None, "source": "probe"}]
    assert o.derive_overall(grant="live", rows=rows, now_ms=NOW, expires_at_ms=None)["status"] == "connected"


def test_session_rejection_while_live_is_failed():
    rows = [{"status": "needs-auth", "error": "Sign-in rejected by the server (TOKEN_EXPIRED) — re-authenticate.", "source": "session"}]
    out = o.derive_overall(grant="live", rows=rows, now_ms=NOW, expires_at_ms=None)
    assert out == {"status": "failed", "error": "Token rejected by the server while signed in (TOKEN_EXPIRED).", "grant": "live"}


def test_live_without_rows_is_unchecked():
    assert o.derive_overall(grant="live", rows=[], now_ms=NOW, expires_at_ms=None)["status"] is None


def test_non_oauth_server_uses_rows_only():
    rows = [{"status": "pending", "error": None, "source": "probe"}, {"status": "connected", "error": None, "source": "probe"}]
    assert o.derive_overall(grant="none", rows=rows, now_ms=NOW, expires_at_ms=None) == {"status": "connected", "error": None, "grant": "none"}
    assert o.derive_overall(grant="none", rows=[], now_ms=NOW, expires_at_ms=None)["status"] is None
