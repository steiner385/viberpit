"""Tests for ccswitch.gitrepos — clones + gh-backed repo/branch listing."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ccswitch import gitrepos


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def _make_remote(path: Path) -> None:
    """Create a bare repo with one commit, acting as the clone source."""
    work = path.parent / (path.name + "-work")
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", str(work), "-b", "main"], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=str(work))
    _git("config", "user.name", "Test", cwd=str(work))
    (work / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=str(work))
    _git("commit", "-m", "init", cwd=str(work))
    _git("remote", "add", "origin", str(path), cwd=str(work))
    _git("push", "origin", "main", cwd=str(work))


class CloneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "GitHub"  # repo_root
        self.root.mkdir()
        self.remote = self.base / "remote.git"
        _make_remote(self.remote)
        self.url = str(self.remote)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clone_path_layout(self) -> None:
        p = gitrepos.clone_path(str(self.root), "cairnea/KinDash")
        self.assertEqual(
            p, self.root / ".ccswitch" / "clones" / "cairnea" / "KinDash"
        )

    def test_ensure_clone_creates_detached_clone(self) -> None:
        dest = gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        expected = self.root / ".ccswitch" / "clones" / "acme" / "widget"
        self.assertEqual(Path(dest), expected)
        self.assertTrue((expected / ".git").exists())
        # HEAD is detached — symbolic-ref fails on a detached head.
        r = subprocess.run(
            ["git", "-C", dest, "symbolic-ref", "-q", "HEAD"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0, "expected detached HEAD")

    def test_ensure_clone_is_idempotent(self) -> None:
        first = gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        second = gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        self.assertEqual(first, second)
        self.assertTrue((Path(second) / ".git").exists())

    def test_ensure_clone_refresh_fetches_origin_only(self) -> None:
        # Refreshing an existing clone must fetch ONLY origin, never --all:
        # a spawned session can leave stray remotes on this shared clone, and
        # `--all` would fetch (or fail on) each one, slowing/breaking every
        # future automation spawn (issue #567).
        gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        calls: list[list[str]] = []
        real = gitrepos._run_git

        def spy(args, **kw):
            calls.append(list(args))
            return real(args, **kw)

        with mock.patch.object(gitrepos, "_run_git", spy):
            gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        fetches = [a for a in calls if "fetch" in a]
        self.assertTrue(fetches, "expected a refresh fetch on the existing clone")
        for a in fetches:
            self.assertIn("origin", a)
            self.assertNotIn("--all", a)

    def test_ensure_clone_failure_raises(self) -> None:
        with self.assertRaises(gitrepos.CloneError):
            gitrepos.ensure_clone(
                str(self.root), "acme/nope", str(self.base / "does-not-exist.git")
            )

    def test_find_local_reference_matches_same_origin(self) -> None:
        # A user checkout directly under repo_root with the same origin URL.
        local = self.root / "widget"
        subprocess.run(
            ["git", "clone", self.url, str(local)], check=True, capture_output=True
        )
        ref = gitrepos.find_local_reference(str(self.root), self.url)
        self.assertEqual(ref, str(local))

    def test_find_local_reference_ignores_hidden_and_mismatch(self) -> None:
        # hidden dir should be skipped even if it matches
        hidden = self.root / ".ccswitch-fake"
        subprocess.run(["git", "clone", self.url, str(hidden)], check=True, capture_output=True)
        # a non-matching repo
        other_remote = self.base / "other.git"
        _make_remote(other_remote)
        other = self.root / "other"
        subprocess.run(["git", "clone", str(other_remote), str(other)], check=True, capture_output=True)
        ref = gitrepos.find_local_reference(str(self.root), self.url)
        self.assertIsNone(ref)

    def test_find_local_reference_uses_reference_when_cloning(self) -> None:
        local = self.root / "widget"
        subprocess.run(["git", "clone", self.url, str(local)], check=True, capture_output=True)
        # ensure_clone should succeed and produce a working, independent clone
        dest = gitrepos.ensure_clone(str(self.root), "acme/widget", self.url)
        self.assertTrue((Path(dest) / ".git").exists())
        # --dissociate => no alternates pointer left behind
        alternates = Path(dest) / ".git" / "objects" / "info" / "alternates"
        self.assertFalse(alternates.exists())


class UrlNormalizationTests(unittest.TestCase):
    def test_scp_and_https_equal(self) -> None:
        a = gitrepos._normalize_remote_url("git@github.com:owner/name.git")
        b = gitrepos._normalize_remote_url("https://github.com/owner/name")
        self.assertEqual(a, b)
        self.assertEqual(a, "github.com/owner/name")

    def test_trailing_git_and_slash_stripped(self) -> None:
        self.assertEqual(
            gitrepos._normalize_remote_url("https://github.com/owner/name.git/"),
            "github.com/owner/name",
        )

    def test_host_lowercased(self) -> None:
        self.assertEqual(
            gitrepos._normalize_remote_url("https://GitHub.com/Owner/Name"),
            "github.com/Owner/Name",
        )


# ---------------------------------------------------------------------------
# gh-backed listing (subprocess monkeypatched)
# ---------------------------------------------------------------------------


_REPOS_JSON = json.dumps(
    {
        "data": {
            "viewer": {
                "repositories": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "nameWithOwner": "cairnea/KinDash",
                            "name": "KinDash",
                            "owner": {"login": "cairnea"},
                            "isPrivate": True,
                            "url": "https://github.com/cairnea/KinDash",
                            "pushedAt": "2026-06-20T00:00:00Z",
                            "defaultBranchRef": {"name": "main"},
                        },
                        {
                            "nameWithOwner": "acme/widget",
                            "name": "widget",
                            "owner": {"login": "acme"},
                            "isPrivate": False,
                            "url": "https://github.com/acme/widget",
                            "pushedAt": "2026-06-19T00:00:00Z",
                            "defaultBranchRef": None,
                        },
                    ],
                }
            }
        }
    }
)


class GhListingTests(unittest.TestCase):
    def test_list_accessible_repos_parses_and_strips_token(self) -> None:
        captured: dict = {}

        def fake_run(args, capture_output, text, env, input=None):
            captured["args"] = args
            captured["env"] = env
            return subprocess.CompletedProcess(args, 0, stdout=_REPOS_JSON, stderr="")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "stale-token"}):
            with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
                repos = gitrepos.list_accessible_repos()

        self.assertEqual(captured["args"][0], "gh")
        # GITHUB_TOKEN must be stripped from the subprocess env.
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["nameWithOwner"], "cairnea/KinDash")
        self.assertEqual(repos[0]["owner"], "cairnea")
        self.assertTrue(repos[0]["isPrivate"])
        self.assertEqual(repos[0]["defaultBranch"], "main")
        self.assertIsNone(repos[1]["defaultBranch"])

    def test_list_accessible_repos_excludes_archived(self) -> None:
        repos_json = json.dumps(
            {
                "data": {
                    "viewer": {
                        "repositories": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "nameWithOwner": "acme/active",
                                    "name": "active",
                                    "owner": {"login": "acme"},
                                    "isPrivate": False,
                                    "isArchived": False,
                                    "url": "https://github.com/acme/active",
                                    "pushedAt": "2026-06-20T00:00:00Z",
                                    "defaultBranchRef": {"name": "main"},
                                },
                                {
                                    "nameWithOwner": "acme/old",
                                    "name": "old",
                                    "owner": {"login": "acme"},
                                    "isPrivate": False,
                                    "isArchived": True,
                                    "url": "https://github.com/acme/old",
                                    "pushedAt": "2025-01-01T00:00:00Z",
                                    "defaultBranchRef": {"name": "main"},
                                },
                            ],
                        }
                    }
                }
            }
        )

        no_orgs = json.dumps(
            {
                "data": {
                    "viewer": {
                        "organizations": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        )
        calls: list = []

        def fake_run(args, capture_output, text, env, input=None):
            calls.append(args)
            # The org-membership discovery query is a separate gh call; answer it
            # with an empty org list so only the viewer.repositories query feeds
            # the chooser here.
            out = no_orgs if "organizations" in " ".join(args) else repos_json
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        # The repositories GraphQL query must request the archived flag, otherwise
        # the API never returns it and nothing would ever be filtered.
        self.assertTrue(any("isArchived" in " ".join(c) for c in calls))
        # Archived repos must not appear in the chooser.
        self.assertEqual([r["nameWithOwner"] for r in repos], ["acme/active"])

    def test_list_accessible_repos_paginates(self) -> None:
        page1 = json.dumps(
            {
                "data": {
                    "viewer": {
                        "repositories": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                            "nodes": [
                                {
                                    "nameWithOwner": "a/one",
                                    "name": "one",
                                    "owner": {"login": "a"},
                                    "isPrivate": False,
                                    "url": "u1",
                                    "pushedAt": "t1",
                                    "defaultBranchRef": {"name": "main"},
                                }
                            ],
                        }
                    }
                }
            }
        )
        page2 = json.dumps(
            {
                "data": {
                    "viewer": {
                        "repositories": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "nameWithOwner": "a/two",
                                    "name": "two",
                                    "owner": {"login": "a"},
                                    "isPrivate": False,
                                    "url": "u2",
                                    "pushedAt": "t2",
                                    "defaultBranchRef": {"name": "main"},
                                }
                            ],
                        }
                    }
                }
            }
        )
        no_orgs = json.dumps(
            {
                "data": {
                    "viewer": {
                        "organizations": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        )
        calls: list = []

        def fake_run(args, capture_output, text, env, input=None):
            calls.append(args)
            joined = " ".join(args)
            if "organizations" in joined:
                return subprocess.CompletedProcess(args, 0, stdout=no_orgs, stderr="")
            out = page2 if "cursor=CURSOR1" in joined else page1
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        # Both pages must be surfaced (order is by pushedAt, not fetch order).
        self.assertEqual({r["nameWithOwner"] for r in repos}, {"a/one", "a/two"})
        # The second page was fetched by following the endCursor.
        self.assertTrue(any("cursor=CURSOR1" in " ".join(c) for c in calls))

    def test_list_accessible_repos_gh_failure_raises(self) -> None:
        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not logged in")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            with self.assertRaises(gitrepos.GhError):
                gitrepos.list_accessible_repos()

    def test_list_accessible_repos_missing_gh_raises(self) -> None:
        def fake_run(args, capture_output, text, env, input=None):
            raise FileNotFoundError("gh")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            with self.assertRaises(gitrepos.GhError):
                gitrepos.list_accessible_repos()

    def test_list_remote_branches_parses_and_strips_token(self) -> None:
        envs: list = []

        def fake_run(args, capture_output, text, env, input=None):
            envs.append(env)
            if "branches" in args[2]:  # repos/<o>/<n>/branches
                return subprocess.CompletedProcess(args, 0, stdout="main\nfeature-x\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "stale-token"}):
            with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
                result = gitrepos.list_remote_branches("acme/widget")

        self.assertEqual(result["default"], "main")
        self.assertEqual(result["branches"], ["main", "feature-x"])
        for env in envs:
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_list_remote_branches_bad_name(self) -> None:
        with self.assertRaises(ValueError):
            gitrepos.list_remote_branches("no-slash")

    def test_list_remote_branches_gh_failure_raises(self) -> None:
        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="404")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            with self.assertRaises(gitrepos.GhError):
                gitrepos.list_remote_branches("acme/widget")


class StaleCredentialRegistryTests(unittest.TestCase):
    """The auth-error classification + stale-credential registry (issue #670)."""

    def setUp(self) -> None:
        # The registry is process-global; isolate every test.
        gitrepos._stale_credentials.clear()

    def tearDown(self) -> None:
        gitrepos._stale_credentials.clear()

    def test_is_auth_error_matches_credential_failures(self) -> None:
        for msg in (
            "remote: Invalid username or token. Password authentication is not supported",
            "fatal: Authentication failed for 'https://github.com/acme/widget.git/'",
            "remote: HTTP 403 Forbidden",
            "could not read Username for 'https://github.com': terminal prompts disabled",
        ):
            self.assertTrue(gitrepos.is_auth_error(msg), msg)

    def test_is_auth_error_ignores_transient_failures(self) -> None:
        for msg in (
            "fatal: unable to access 'https://github.com/acme/widget.git/': "
            "Could not resolve host: github.com",
            "error: RPC failed; curl 28 Operation timed out",
            "",
            None,
        ):
            self.assertFalse(gitrepos.is_auth_error(msg), repr(msg))

    def test_record_auth_failure_registers_repo(self) -> None:
        gitrepos.record_fetch_result(
            "acme/widget", ok=False,
            stderr="remote: Invalid username or token. Password authentication is not supported")
        self.assertTrue(gitrepos.has_stale_credentials("acme/widget"))
        alerts = gitrepos.stale_credential_repos()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["repo"], "acme/widget")
        self.assertIn("Invalid username or token", alerts[0]["error"])
        self.assertEqual(alerts[0]["settingsUrl"], gitrepos.token_settings_url())
        self.assertIsInstance(alerts[0]["since"], float)

    def test_record_transient_failure_does_not_register(self) -> None:
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="Could not resolve host: github.com")
        self.assertFalse(gitrepos.has_stale_credentials("acme/widget"))
        self.assertEqual(gitrepos.stale_credential_repos(), [])

    def test_success_auto_clears_prior_alert(self) -> None:
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="Authentication failed")
        self.assertTrue(gitrepos.has_stale_credentials("acme/widget"))
        gitrepos.record_fetch_result("acme/widget", ok=True)
        self.assertFalse(gitrepos.has_stale_credentials("acme/widget"))
        self.assertEqual(gitrepos.stale_credential_repos(), [])

    def test_repeated_failure_preserves_first_seen_timestamp(self) -> None:
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="Authentication failed")
        first = gitrepos.stale_credential_repos()[0]["since"]
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="remote: HTTP 403 Forbidden")
        second = gitrepos.stale_credential_repos()[0]
        self.assertEqual(second["since"], first)
        self.assertIn("403", second["error"])

    def test_clear_stale_credentials(self) -> None:
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="Authentication failed")
        self.assertTrue(gitrepos.clear_stale_credentials("acme/widget"))
        self.assertFalse(gitrepos.clear_stale_credentials("acme/widget"))
        self.assertFalse(gitrepos.has_stale_credentials("acme/widget"))

    def test_ensure_clone_auth_fetch_failure_flags_repo(self) -> None:
        """A refresh fetch that fails auth flags the repo but still returns the
        (stale) clone path — read-only callers keep working, new work pauses."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "GitHub"
        remote = Path(tmp.name) / "remote.git"
        root.mkdir()
        _make_remote(remote)
        # Create the clone once (succeeds, clears registry).
        gitrepos.ensure_clone(str(root), "acme/widget", str(remote))
        self.assertFalse(gitrepos.has_stale_credentials("acme/widget"))

        # Now make the refresh fetch fail with an auth error.
        real_run = gitrepos._run_git

        def fake_run(args):
            if "fetch" in args:
                return subprocess.CompletedProcess(
                    args, 1, stdout="",
                    stderr="remote: Invalid username or token. "
                           "Password authentication is not supported")
            return real_run(args)

        with mock.patch.object(gitrepos, "_run_git", side_effect=fake_run):
            with self.assertLogs("ccswitch.gitrepos", level="ERROR") as cm:
                dest = gitrepos.ensure_clone(str(root), "acme/widget", str(remote))
        self.assertTrue(Path(dest, ".git").exists())
        self.assertTrue(gitrepos.has_stale_credentials("acme/widget"))
        self.assertTrue(any("credential refresh needed" in line for line in cm.output))

    def test_ensure_clone_successful_refresh_clears_alert(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "GitHub"
        remote = Path(tmp.name) / "remote.git"
        root.mkdir()
        _make_remote(remote)
        gitrepos.ensure_clone(str(root), "acme/widget", str(remote))
        # Pre-seed a stale alert, then a clean refresh fetch must clear it.
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="Authentication failed")
        gitrepos.ensure_clone(str(root), "acme/widget", str(remote))
        self.assertFalse(gitrepos.has_stale_credentials("acme/widget"))


if __name__ == "__main__":
    unittest.main()
