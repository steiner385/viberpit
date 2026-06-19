import threading
import time
import unittest
from unittest import mock
from tests.helpers import temp_env, usage


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                              "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class TestConcurrency(unittest.TestCase):
    def test_parallel_picks_do_not_double_assign_when_one_clear_winner(self):
        with temp_env():
            from ccswitch import accounts
            _seed("alpha"); _seed("beta"); _seed("gamma")
            # beta has by far the most headroom; with reservations, only ONE picker should get beta.
            def fake_usage(n):
                return {"alpha": usage(session=70.0), "beta": usage(session=5.0), "gamma": usage(session=72.0)}[n]
            picks = []
            lock = threading.Lock()
            def worker():
                with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                    name = accounts.pick_account(accounts.load_config())
                with lock:
                    picks.append(name)
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(len(picks), 3)
            self.assertEqual(picks.count("beta"), 1)  # reservation prevents double-assign
