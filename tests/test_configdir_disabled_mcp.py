"""trust_project writes projects[cwd].disabledMcpServers; compose() wipes stale lists (R3)."""
import json
import unittest

from tests.helpers import temp_env


class TestDisabledMcp(unittest.TestCase):
    def _seed(self, name="alpha"):
        from viberpit import paths
        from viberpit.creds import write_json_secure
        write_json_secure(paths.slot_path(name), {"accessToken": "A", "expiresAt": 9})
        write_json_secure(paths.store_claude_json_path(name), {"userID": "u"})

    def _projects(self, name="alpha"):
        from viberpit import paths
        return json.loads((paths.account_cfg_dir(name) / ".claude.json").read_text())["projects"]

    def test_writes_list(self):
        with temp_env():
            from viberpit import configdir
            self._seed()
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1", disabled_mcp=["gh", "plugin:p:s"])
            self.assertEqual(self._projects()["/w/1"]["disabledMcpServers"], ["gh", "plugin:p:s"])

    def test_none_leaves_existing_key(self):
        with temp_env():
            from viberpit import configdir
            self._seed()
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1", disabled_mcp=["x"])
            configdir.trust_project("alpha", "/w/1")
            self.assertEqual(self._projects()["/w/1"]["disabledMcpServers"], ["x"])

    def test_empty_list_clears(self):
        with temp_env():
            from viberpit import configdir
            self._seed()
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1", disabled_mcp=["x"])
            configdir.trust_project("alpha", "/w/1", disabled_mcp=[])
            self.assertEqual(self._projects()["/w/1"]["disabledMcpServers"], [])

    def test_distinct_worktrees_keep_separate_lists(self):
        with temp_env():
            from viberpit import configdir
            self._seed()
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1", disabled_mcp=["a"])
            configdir.trust_project("alpha", "/w/2", disabled_mcp=["b"])
            p = self._projects()
            self.assertEqual(p["/w/1"]["disabledMcpServers"], ["a"])
            self.assertEqual(p["/w/2"]["disabledMcpServers"], ["b"])

    def test_compose_wipes_stale_cli_written_list(self):
        """R3: with no list written (kill switch off), a previously written list
        must not leak into the next launch — compose() rewrites .claude.json."""
        with temp_env():
            from viberpit import configdir
            self._seed()
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1", disabled_mcp=["gh"])
            configdir.compose("alpha")
            configdir.trust_project("alpha", "/w/1")
            self.assertNotIn("disabledMcpServers", self._projects()["/w/1"])
