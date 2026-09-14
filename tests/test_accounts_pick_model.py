"""pick_account_detailed forwards *model* to the selector."""
import unittest
from unittest.mock import patch

from tests.helpers import temp_env, usage

from ccswitch import accounts
from ccswitch.config import load_config
from ccswitch.selector import Picked


class TestPickModel(unittest.TestCase):
    def test_model_reaches_select_picked(self):
        with temp_env():
            cfg = load_config()
            seen = {}

            def fake_select_picked(usages, reserved, cfg_, now, exclude, load, *a, **kw):
                seen["model"] = kw.get("model")
                return Picked(name="alpha", optimistic=False)

            with patch.object(accounts, "select_picked", fake_select_picked), \
                 patch.object(accounts, "usage_for", lambda n: usage()), \
                 patch.object(accounts, "_account_names", lambda: ["alpha"]):
                # require_engine=None keeps the registry (and its engine filter) out
                # of the arrangement: the assertion is the forwarding, not eligibility.
                picked = accounts.pick_account_detailed(
                    cfg, reserve=False, require_engine=None, model="fable")
        self.assertEqual(picked.name, "alpha")
        self.assertEqual(seen["model"], "fable")

    def test_pick_account_forwards_model(self):
        with temp_env():
            cfg = load_config()
            seen = {}

            def fake_detailed(cfg_, exclude=frozenset(), load=None, **kw):
                seen["model"] = kw.get("model")
                return Picked(name="alpha", optimistic=False)

            with patch.object(accounts, "pick_account_detailed", fake_detailed):
                self.assertEqual(accounts.pick_account(cfg, model="sonnet"), "alpha")
        self.assertEqual(seen["model"], "sonnet")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
