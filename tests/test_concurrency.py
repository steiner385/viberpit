import threading
import time
import unittest
from unittest import mock
from tests.helpers import temp_env, usage


def _make_token(expires_delta_ms: int, access: str = "A", refresh: str = "R") -> dict:
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int(time.time() * 1000) + expires_delta_ms,
        "subscriptionType": "max",
    }


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                              "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class TestSyncBackLock(unittest.TestCase):
    """Finding 1 — sync_back must hold _account_lock during the slot read-modify-write."""

    def test_sync_back_holds_account_lock_during_slot_write(self):
        """Verify that _account_lock is acquired inside sync_back by monkeypatching
        _account_lock to return a wrapper that records acquisitions.

        This is the deterministic "proof the lock is held" variant: we don't need
        to reproduce the actual race — we just confirm the lock wraps the write.
        """
        with temp_env():
            from ccswitch import accounts, paths
            from ccswitch.creds import write_json_secure

            # Seed a slot so read_credentials doesn't short-circuit.
            write_json_secure(paths.slot_path("alpha"), _make_token(9_000_000))
            write_json_secure(paths.shared_mcp_path(), {})

            # Write a live .credentials.json so sync_back has something to read.
            cfg_dir = paths.account_cfg_dir("alpha")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            import json
            (cfg_dir / ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": _make_token(9_000_000, access="live-A", refresh="live-R"),
            }))

            # Track whether the lock is held when write_credentials is called.
            lock_held_during_write: list[bool] = []
            real_lock = accounts._account_lock("alpha")

            original_write = accounts.write_credentials

            def spy_write(path, blob):
                # Is the real lock currently held (non-blocking acquire returns False if held)?
                acquired = real_lock.acquire(blocking=False)
                if acquired:
                    real_lock.release()
                    lock_held_during_write.append(False)
                else:
                    lock_held_during_write.append(True)  # lock IS held — correct
                return original_write(path, blob)

            with mock.patch.object(accounts, "write_credentials", side_effect=spy_write):
                accounts.sync_back("alpha")

            self.assertTrue(
                lock_held_during_write,
                "write_credentials was never called in sync_back",
            )
            self.assertTrue(
                all(lock_held_during_write),
                f"_account_lock was NOT held during write_credentials call(s): {lock_held_during_write}",
            )

    def test_sync_back_concurrent_newer_token_wins(self):
        """Deterministic interleaved race: two sync_back calls — one with a NEWER
        expiresAt (from cfgdir) and one with an OLDER one.  The slot must contain
        the newer token after both complete, regardless of which write_credentials
        call goes second.

        Strategy: patch split_credentials (the cfgdir read that happens OUTSIDE
        the lock) to return per-thread blobs.  Use a threading.Barrier to ensure
        BOTH threads complete their outside-lock read before EITHER enters the
        locked write section.  Then let the writes race in order: first the
        "older" thread writes (so the slot holds the older token momentarily),
        then the "newer" thread overwrites with the newer token.

        With the lock + merge_keep_later the newer token always survives.
        """
        import json

        with temp_env():
            from ccswitch import accounts, paths
            from ccswitch.creds import write_json_secure

            # Seed the initial slot.
            write_json_secure(paths.slot_path("beta"), _make_token(1_000_000, access="init"))
            write_json_secure(paths.shared_mcp_path(), {})

            # Write any .credentials.json so live_path.exists() passes in sync_back.
            cfg_dir = paths.account_cfg_dir("beta")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": _make_token(1_000_000)}))

            now_ms = int(time.time() * 1000)
            older_ms = now_ms + 1_000_000   # ~16 min
            newer_ms = now_ms + 9_000_000   # ~2.5 h

            # Map thread-id → blob that split_credentials should return for that call.
            thread_blobs: dict[int, dict] = {}
            thread_blobs_lock = threading.Lock()

            # A barrier so both threads have "read cfgdir" (i.e. decided their blob)
            # before either proceeds to the locked slot write.
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            real_split = accounts.split_credentials

            def fake_split(raw: dict):
                """Return the per-thread blob registered before the barrier."""
                with thread_blobs_lock:
                    blob = thread_blobs.get(threading.current_thread().ident, {})
                return blob, None  # (claude_oauth, mcp)

            def _run_sync_back(expires_ms: int, access: str):
                try:
                    # Register this thread's "read from cfgdir" result.
                    blob = _make_token(expires_ms - int(time.time() * 1000),
                                       access=access, refresh=access + "-R")
                    with thread_blobs_lock:
                        thread_blobs[threading.current_thread().ident] = blob
                    # Both threads synchronise here before either write.
                    barrier.wait(timeout=5)
                    accounts.sync_back("beta")
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(accounts, "split_credentials", side_effect=fake_split):
                t_older = threading.Thread(target=_run_sync_back, args=(older_ms, "older-access"))
                t_newer = threading.Thread(target=_run_sync_back, args=(newer_ms, "newer-access"))
                t_older.start()
                t_newer.start()
                t_older.join(timeout=10)
                t_newer.join(timeout=10)

            self.assertEqual(errors, [], f"threads raised: {errors}")

            slot = json.loads(paths.slot_path("beta").read_text())
            # merge_keep_later + lock guarantees the newer expiresAt wins.
            self.assertEqual(
                slot.get("accessToken"), "newer-access",
                f"expected newer-access in slot, got: {slot}",
            )


class TestConcurrency(unittest.TestCase):
    def test_parallel_picks_do_not_double_assign_when_one_clear_winner(self):
        with temp_env():
            from ccswitch import accounts
            _seed("alpha"); _seed("beta"); _seed("gamma")
            # beta has by far the most headroom; with reservations, only ONE picker should get beta.
            # Mock is applied OUTSIDE the threads so only one patch+unpatch happens on the
            # main thread — mock.patch.object is not thread-safe when multiple threads each
            # try to patch/unpatch the same attribute concurrently (the restoration order is
            # non-deterministic and can leave the attribute pointing at the fake after joining).
            def fake_usage(n, **_kwargs):
                return {"alpha": usage(session=70.0), "beta": usage(session=5.0), "gamma": usage(session=72.0)}[n]
            picks = []
            lock = threading.Lock()
            def worker():
                name = accounts.pick_account(accounts.load_config())
                with lock:
                    picks.append(name)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                threads = [threading.Thread(target=worker) for _ in range(3)]
                for t in threads: t.start()
                for t in threads: t.join()
            self.assertEqual(len(picks), 3)
            self.assertEqual(picks.count("beta"), 1)  # reservation prevents double-assign
