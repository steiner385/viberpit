"""Cross-process refresh lock (_refresh_guard) — prevents single-use-token races.

Anthropic refresh tokens are single-use: two refreshers (web daemon / edge /
ccswitch CLI) racing would both present the same token, the first rotates it and
the second gets invalid_grant → false needs-reauth. _refresh_guard nests an
flock (cross-process) inside the per-process threading.Lock.
"""
import fcntl
import os
import threading
import time
from unittest import mock

from tests.helpers import temp_env


def _make_token(expires_delta_ms: int, access="A", refresh="R") -> dict:
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int(time.time() * 1000) + expires_delta_ms,
        "subscriptionType": "max",
    }


def test_refresh_guard_holds_crossprocess_flock():
    """While the guard is held, a second fd's non-blocking flock must fail —
    proving the lock excludes a separate process (which uses a separate fd)."""
    with temp_env():
        from ccswitch import accounts, paths

        with accounts._refresh_guard("alpha"):
            lock_path = paths.store_dir() / "alpha.refresh.lock"
            assert lock_path.exists(), "guard did not create the lock file"
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                raised = False
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    raised = True
                assert raised, "second fd acquired the lock while guard was held"
            finally:
                os.close(fd)


def test_refresh_guard_released_after_block():
    """After the guard exits, the lock is free again (a fresh fd can acquire it)."""
    with temp_env():
        from ccswitch import accounts, paths

        with accounts._refresh_guard("beta"):
            pass
        lock_path = paths.store_dir() / "beta.refresh.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def test_concurrent_ensure_valid_token_refreshes_once():
    """Two threads racing an expired token must trigger exactly ONE refresh; the
    loser re-reads the freshly-persisted rotated token under the guard."""
    with temp_env():
        from ccswitch import accounts, paths
        from ccswitch.creds import write_json_secure

        write_json_secure(paths.slot_path("gamma"), _make_token(-10_000, access="old", refresh="R0"))

        calls = []
        barrier = threading.Barrier(2)

        def fake_refresh(refresh_token, prior, **_kw):
            calls.append(refresh_token)
            time.sleep(0.05)  # widen the window so a second un-guarded caller would race in
            return {**prior, "accessToken": "new", "refreshToken": "R1",
                    "expiresAt": int(time.time() * 1000) + 9_000_000}

        results = []

        def worker():
            barrier.wait(timeout=5)
            results.append(accounts.ensure_valid_token("gamma")["accessToken"])

        with mock.patch.object(accounts.oauth, "refresh_access_token", side_effect=fake_refresh):
            ts = [threading.Thread(target=worker) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=10)

        assert len(calls) == 1, f"expected exactly one refresh, got {len(calls)}"
        assert results == ["new", "new"], f"both callers should see the rotated token: {results}"
