"""Credential envelope rules + runner store (#2629)."""
import json
import os
import stat
import unittest

from tests.helpers import temp_env

from viberpit import credential_envelope as ce
from viberpit.credential_envelope import Envelope, EnvelopeStore, accepts


def _env(acct="a", gen=1, kind=ce.SETUP_TOKEN, token="tok", exp=None):
    return Envelope(acct, kind, token, exp, gen)


class TestRules(unittest.TestCase):
    def test_generation_is_monotonic(self):
        self.assertTrue(accepts(None, _env(gen=1)))
        self.assertTrue(accepts(_env(gen=2), _env(gen=3)))
        self.assertTrue(accepts(_env(gen=2), _env(gen=2)))
        self.assertFalse(accepts(_env(gen=2), _env(gen=1)))

    def test_repr_hides_token(self):
        self.assertNotIn("SECRET", repr(_env(token="SECRET")))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            Envelope("a", "bogus", "t", None, 1)


class TestStore(unittest.TestCase):
    def test_rollback_refused_and_ack_reports_highest(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            self.assertEqual(s.sync([_env(gen=5, token="new")], 60, now=100), {"a": 5})
            self.assertEqual(s.sync([_env(gen=4, token="old")], 60, now=101), {"a": 5})
            self.assertEqual(s.launch_token("a", now=102), "new")

    def test_lease_lapse_fails_closed_but_still_holds(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            s.sync([_env()], 30, now=100)
            self.assertEqual(s.launch_token("a", now=129), "tok")
            self.assertIsNone(s.launch_token("a", now=131))
            self.assertTrue(s.holds("a"))  # never falls back to the mount

    def test_awaiting_renewal_only_once_something_was_vended(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            self.assertFalse(s.awaiting_renewal(now=100))  # never brokered here
            s.sync([_env()], 60, now=100)
            self.assertFalse(s.awaiting_renewal(now=159))  # lease live
            self.assertTrue(s.awaiting_renewal(now=161))   # restart outlived the lease
            s.sync([], 60, now=200)                        # authority vends nothing
            self.assertFalse(s.awaiting_renewal(now=300))

    def test_account_missing_from_push_is_dropped(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            s.sync([_env("a"), _env("b")], 60, now=100)
            s.sync([_env("b")], 60, now=110)
            self.assertFalse(s.holds("a"))

    def test_oauth_access_refused_within_skew(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            s.sync([_env(kind=ce.OAUTH_ACCESS, exp=1000)], 10_000, now=100)
            self.assertEqual(s.launch_token("a", skew_s=300, now=600), "tok")
            self.assertIsNone(s.launch_token("a", skew_s=300, now=701))

    def test_cache_survives_restart_and_is_0600(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c")
            s.sync([_env(gen=3)], 60, now=100)
            path = d / "c" / "envelopes.json"
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            s2 = EnvelopeStore(d / "c")
            self.assertEqual(s2.launch_token("a", now=120), "tok")
            self.assertIsNone(s2.launch_token("a", now=161))  # lease persisted too

    def test_corrupt_cache_is_empty(self):
        with temp_env() as d:
            (d / "c").mkdir()
            (d / "c" / "envelopes.json").write_text("{nope")
            self.assertFalse(EnvelopeStore(d / "c").holds("a"))
            json.dumps({})


class TestRemoteControlEnvelope(unittest.TestCase):
    """The Remote Control credential (claude.ai access token + org UUID) rides a second,
    independent store: its own cache file, generations and lease — never the inference one."""

    def _rc(self, gen=1, token="rc-tok", exp=10_000.0, org="org-1"):
        return Envelope("a", ce.OAUTH_ACCESS, token, exp, gen, org_uuid=org)

    def test_org_uuid_round_trips_and_is_optional(self):
        env = self._rc()
        self.assertEqual(Envelope.from_dict(json.loads(json.dumps(env.__dict__))), env)
        old = {"account": "a", "kind": ce.SETUP_TOKEN, "token": "t", "expires_at": None,
               "generation": 1}
        self.assertIsNone(Envelope.from_dict(old).org_uuid)   # an old edge's wire

    def test_launch_envelope_carries_the_org_and_fails_closed(self):
        with temp_env() as d:
            s = EnvelopeStore(d / "c", cache_file=ce.RC_CACHE_FILE)
            s.sync([self._rc()], 60, now=100)
            got = s.launch_envelope("a", skew_s=120, now=110)
            self.assertEqual((got.token, got.org_uuid), ("rc-tok", "org-1"))
            self.assertIsNone(s.launch_envelope("a", skew_s=120, now=161))     # lease lapsed
            s.sync([self._rc(exp=200)], 60, now=150)
            self.assertIsNone(s.launch_envelope("a", skew_s=120, now=151))     # near expiry

    def test_its_own_cache_file_0600_and_generations(self):
        with temp_env() as d:
            inference = EnvelopeStore(d / "c")
            rc = EnvelopeStore(d / "c", cache_file=ce.RC_CACHE_FILE)
            inference.sync([_env(gen=7)], 60, now=100)
            self.assertEqual(rc.sync([self._rc(gen=2)], 60, now=100), {"a": 2})
            self.assertEqual(rc.sync([self._rc(gen=1, token="old")], 60, now=101), {"a": 2})
            path = d / "c" / ce.RC_CACHE_FILE
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            reloaded = EnvelopeStore(d / "c", cache_file=ce.RC_CACHE_FILE)
            self.assertEqual(reloaded.launch_envelope("a", now=102).org_uuid, "org-1")
            self.assertEqual(EnvelopeStore(d / "c").launch_token("a", now=102), "tok")

    def test_process_wide_stores_are_distinct_and_reset_together(self):
        with temp_env() as d:
            os.environ["VIBERPIT_CLAUDE_CREDS_DIR"] = str(d)
            try:
                ce.reset_runner_store()
                self.assertIsNot(ce.runner_store(), ce.rc_runner_store())
                ce.rc_runner_store().sync([self._rc()], 60)
                self.assertFalse(ce.runner_store().holds("a"))
                ce.reset_runner_store()
                self.assertTrue(ce.rc_runner_store().holds("a"))   # reloaded from its cache
            finally:
                os.environ.pop("VIBERPIT_CLAUDE_CREDS_DIR", None)
                ce.reset_runner_store()


if __name__ == "__main__":
    unittest.main()
