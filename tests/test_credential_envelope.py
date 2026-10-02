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


if __name__ == "__main__":
    unittest.main()
