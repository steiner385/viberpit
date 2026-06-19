"""Tests for the three new path helpers: global_claude_json, store_claude_json_path,
shared_claude_json_path (T007 / paths portion)."""
import unittest
from pathlib import Path
from tests.helpers import temp_env


class TestPathsClaudeJson(unittest.TestCase):
    def test_global_claude_json(self):
        """global_claude_json() is always HOME/.claude.json regardless of env overrides."""
        with temp_env():
            from ccswitch import paths
            result = paths.global_claude_json()
            self.assertEqual(result, Path.home() / ".claude.json")
            self.assertIsInstance(result, Path)

    def test_store_claude_json_path(self):
        """store_claude_json_path(name) lives inside store_dir()."""
        with temp_env():
            from ccswitch import paths
            p = paths.store_claude_json_path("alice")
            self.assertEqual(p, paths.store_dir() / "alice.claude.json")

    def test_shared_claude_json_path(self):
        """shared_claude_json_path() lives inside store_dir()."""
        with temp_env():
            from ccswitch import paths
            p = paths.shared_claude_json_path()
            self.assertEqual(p, paths.store_dir() / "claude.shared.json")

    def test_store_claude_json_path_different_names(self):
        """Each account name gets its own file."""
        with temp_env():
            from ccswitch import paths
            self.assertNotEqual(
                paths.store_claude_json_path("alice"),
                paths.store_claude_json_path("bob"),
            )
