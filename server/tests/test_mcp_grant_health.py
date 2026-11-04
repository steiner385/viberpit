"""mcp_grant_health: durable per-grant refresh health and its transitions (phase 3)."""
from __future__ import annotations

import pytest

from server.mcp_health import grant_health as gh
from server.mcp_health.grant_health import Transition

K = "plugin:railway:railway|https://mcp.railway.com"


def _o(outcome: str, *, key: str = K, label: str = "railway", error: str | None = None) -> dict:
    return {"key": key, "label": label, "outcome": outcome, "error": error}


@pytest.mark.asyncio
async def test_refreshed_sets_live_ok_and_resets_failures(isolated_db):
    assert await gh.record([_o("refreshed")], expired_keys=frozenset()) == []
    row = (await gh.load())[K]
    assert row["state"] == "live"
    assert row["server"] == "railway"
    assert row["consecutiveFailures"] == 0
    assert row["lastOutcome"] == "refreshed"
    assert row["lastError"] is None
    assert row["lastOkAt"].endswith("Z") and row["lastAttemptAt"].endswith("Z")
    assert row["lastOkAt"] == row["lastAttemptAt"]
    assert row["alertedFor"] is None


@pytest.mark.asyncio
async def test_transient_increments_failures_and_keeps_state(isolated_db):
    await gh.record([_o("refreshed")], expired_keys=frozenset())
    ok_at = (await gh.load())[K]["lastOkAt"]
    assert await gh.record([_o("transient", error="HTTP 503")], expired_keys=frozenset()) == []
    assert await gh.record([_o("transient", error="HTTP 502")], expired_keys=frozenset()) == []
    row = (await gh.load())[K]
    assert row["state"] == "live"
    assert row["consecutiveFailures"] == 2
    assert row["lastOutcome"] == "transient"
    assert row["lastError"] == "HTTP 502"
    assert row["lastOkAt"] == ok_at


@pytest.mark.asyncio
async def test_transient_on_a_fresh_key_has_null_state(isolated_db):
    await gh.record([_o("transient", error="timeout")], expired_keys=frozenset())
    row = (await gh.load())[K]
    assert row["state"] is None
    assert row["consecutiveFailures"] == 1
    assert row["lastOkAt"] is None


@pytest.mark.asyncio
async def test_dead_sets_state_and_alerts_once_after_mark_alerted(isolated_db):
    [t] = await gh.record([_o("dead", error="invalid_grant")], expired_keys=frozenset())
    assert t == Transition(key=K, server="railway", kind="alert_dead", reason=None, generation="never")
    row = (await gh.load())[K]
    assert row["state"] == "dead"
    assert row["lastError"] == "invalid_grant"
    await gh.mark_alerted(K, f"dead:{t.generation}")
    assert (await gh.load())[K]["alertedFor"] == "dead:never"
    assert (await gh.load())[K]["alertedAt"].endswith("Z")
    assert await gh.record([_o("dead", error="invalid_grant")], expired_keys=frozenset()) == []


@pytest.mark.asyncio
async def test_dead_without_mark_alerted_alerts_again(isolated_db):
    """Delivery failed (no mark_alerted): the next tick must retry the alert."""
    await gh.record([_o("dead")], expired_keys=frozenset())
    [t] = await gh.record([_o("dead")], expired_keys=frozenset())
    assert t.kind == "alert_dead"


@pytest.mark.asyncio
async def test_dead_generation_is_last_ok_at(isolated_db):
    await gh.record([_o("refreshed")], expired_keys=frozenset())
    ok_at = (await gh.load())[K]["lastOkAt"]
    [t] = await gh.record([_o("dead")], expired_keys=frozenset())
    assert t.generation == ok_at


@pytest.mark.asyncio
async def test_new_generation_alerts_again(isolated_db):
    [t1] = await gh.record([_o("dead")], expired_keys=frozenset())
    await gh.mark_alerted(K, f"dead:{t1.generation}")
    [healed] = await gh.record([_o("refreshed")], expired_keys=frozenset())
    assert healed.kind == "healed"
    [t2] = await gh.record([_o("dead")], expired_keys=frozenset())
    assert t2.kind == "alert_dead" and t2.generation != t1.generation


@pytest.mark.asyncio
async def test_refreshed_after_transient_heals(isolated_db):
    await gh.record([_o("transient", error="HTTP 503")], expired_keys=frozenset())
    [t] = await gh.record([_o("refreshed")], expired_keys=frozenset())
    assert (t.kind, t.server, t.reason, t.key) == ("healed", "railway", None, K)
    row = (await gh.load())[K]
    assert (row["state"], row["consecutiveFailures"], row["lastError"]) == ("live", 0, None)


@pytest.mark.asyncio
async def test_routine_refresh_of_healthy_grant_never_heals(isolated_db):
    assert await gh.record([_o("refreshed")], expired_keys=frozenset()) == []
    assert await gh.record([_o("refreshed")], expired_keys=frozenset()) == []


@pytest.mark.asyncio
async def test_transient_with_expired_key_alerts_expired_once(isolated_db):
    await gh.record([_o("refreshed")], expired_keys=frozenset())
    ok_at = (await gh.load())[K]["lastOkAt"]
    [t] = await gh.record([_o("transient", error="HTTP 503")], expired_keys=frozenset({K}))
    assert t == Transition(key=K, server="railway", kind="alert_expired", reason="HTTP 503", generation=ok_at)
    assert (await gh.load())[K]["state"] == "expired"
    await gh.mark_alerted(K, f"expired:{t.generation}")
    assert await gh.record([_o("transient", error="HTTP 503")], expired_keys=frozenset({K})) == []
    [healed] = await gh.record([_o("refreshed")], expired_keys=frozenset())
    assert healed.kind == "healed"


@pytest.mark.asyncio
async def test_transient_not_expired_never_alerts(isolated_db):
    assert await gh.record([_o("transient", error="x")], expired_keys=frozenset({"other"})) == []


@pytest.mark.asyncio
async def test_expired_alert_does_not_suppress_dead_alert(isolated_db):
    [t] = await gh.record([_o("transient", error="x")], expired_keys=frozenset({K}))
    await gh.mark_alerted(K, f"expired:{t.generation}")
    [d] = await gh.record([_o("dead")], expired_keys=frozenset())
    assert d.kind == "alert_dead"


@pytest.mark.asyncio
async def test_last_error_is_trimmed_to_500(isolated_db):
    await gh.record([_o("transient", error="e" * 900)], expired_keys=frozenset())
    assert len((await gh.load())[K]["lastError"]) == 500


@pytest.mark.asyncio
async def test_load_filters_by_keys_and_handles_many_outcomes(isolated_db):
    await gh.record([_o("refreshed", key="a", label="A"), _o("dead", key="b", label="B")],
                    expired_keys=frozenset())
    assert set(await gh.load()) == {"a", "b"}
    assert set(await gh.load(["b", "missing"])) == {"b"}
    assert await gh.load([]) == {}


@pytest.mark.asyncio
async def test_empty_outcomes_is_a_noop(isolated_db):
    assert await gh.record([], expired_keys=frozenset()) == []
    assert await gh.load() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (400, {"error": "RT-SECRET-value", "error_description": "AT-SECRET-value RT-SECRET-value"}),
    (503, {"error": "temporarily_unavailable", "access_token": "", "refresh_token": "RT-SECRET-value",
           "error_description": "RT-SECRET-value"}),
])
async def test_stored_error_never_contains_a_token(isolated_db, status, payload):
    """``last_error`` is the refresher's exception text. A hostile endpoint echoing the credential
    (as the code or the description) must not get it into the health row."""
    import json

    from viberpit import mcp_oauth_refresh as r
    bundle = {"serverName": "x", "serverUrl": "https://mcp.example.invalid/mcp", "clientId": "cid",
              "accessToken": "AT-SECRET-value", "refreshToken": "RT-SECRET-value",
              "discoveryState": {"authorizationServerUrl": "https://auth.example.invalid"}}
    fetch = lambda url: {"token_endpoint": "https://auth.example.invalid/token"}  # noqa: E731
    post = lambda url, form, headers: (status, json.dumps(payload).encode())  # noqa: E731
    with pytest.raises(Exception) as ei:
        r.refresh_bundle(bundle, post_form=post, fetch_json=fetch)
    await gh.record([_o("transient", error=str(ei.value))], expired_keys=frozenset())
    stored = (await gh.load())[K]["lastError"]
    assert stored and stored.startswith("token endpoint returned")
    assert "SECRET" not in stored
