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

        def fake_run(args, owner=None):
            if "fetch" in args:
                return subprocess.CompletedProcess(
                    args, 1, stdout="",
                    stderr="remote: Invalid username or token. "
                           "Password authentication is not supported")
            return real_run(args, owner=owner)

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


# ---------------------------------------------------------------------------
# App-installation-token injection for git network operations (issue #887)
# ---------------------------------------------------------------------------


class GitTokenProviderTests(unittest.TestCase):
    """``_git_env`` turns a registered provider's token into git credential env.

    The token must reach git via the ENVIRONMENT only — never argv (world-readable
    via ``ps``) and never the on-disk remote URL.
    """

    def tearDown(self) -> None:
        gitrepos.set_token_provider(None)

    def test_no_provider_registered_inherits_ambient_env(self) -> None:
        # CLI usage: nothing registered, so git keeps whatever credential helper
        # the user's machine configures. Returning None means "inherit".
        gitrepos.set_token_provider(None)
        self.assertIsNone(gitrepos._git_env("acme"))

    def test_provider_token_injects_credential_helper(self) -> None:
        gitrepos.set_token_provider(lambda owner: "ghs_tok123")
        env = gitrepos._git_env("acme")
        assert env is not None
        self.assertEqual(env["CCSWITCH_GIT_TOKEN"], "ghs_tok123")
        # Two entries: clear the ambient helper list, then install ours.
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "credential.helper")
        self.assertIn("x-access-token", env["GIT_CONFIG_VALUE_1"])

    def test_helper_value_does_not_embed_the_token(self) -> None:
        # The helper reads the token from CCSWITCH_GIT_TOKEN at call time; baking
        # it into the config value would leak it anywhere config is echoed.
        gitrepos.set_token_provider(lambda owner: "ghs_secret")
        env = gitrepos._git_env("acme")
        assert env is not None
        self.assertNotIn("ghs_secret", env["GIT_CONFIG_VALUE_1"])

    def test_ambient_github_tokens_are_stripped(self) -> None:
        # Mirrors _gh_env: a poisoned ambient token must never win over the
        # App token we just minted.
        gitrepos.set_token_provider(lambda owner: "ghs_tok123")
        with mock.patch.dict(
            os.environ, {"GITHUB_TOKEN": "ghp_bad", "GH_TOKEN": "ghp_worse"}
        ):
            env = gitrepos._git_env("acme")
        assert env is not None
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_provider_returning_none_falls_back_to_ambient(self) -> None:
        # Owner has no App installation — keep working via the ambient helper
        # rather than failing closed.
        gitrepos.set_token_provider(lambda owner: None)
        self.assertIsNone(gitrepos._git_env("acme"))

    def test_provider_exception_falls_back_to_ambient(self) -> None:
        # A broken provider (network blip minting the token) must degrade to the
        # previous behavior, not break every fetch in the daemon.
        def boom(owner: str) -> str | None:
            raise RuntimeError("mint failed")

        gitrepos.set_token_provider(boom)
        self.assertIsNone(gitrepos._git_env("acme"))

    def test_provider_receives_the_repo_owner(self) -> None:
        seen: list[str] = []

        def provider(owner: str) -> str | None:
            seen.append(owner)
            return None

        gitrepos.set_token_provider(provider)
        gitrepos._git_env("cairnea")
        self.assertEqual(seen, ["cairnea"])


class OwnerFromRemoteUrlTests(unittest.TestCase):
    """Deriving the App-installation owner from a remote URL.

    Call sites that hold a checkout path rather than an ``<owner>/<name>`` (the
    worktree fetch) need this to reach the same credential env.
    """

    def test_https_url(self) -> None:
        self.assertEqual(
            gitrepos.owner_from_remote_url("https://github.com/cairnea/KinDash.git"),
            "cairnea",
        )

    def test_scp_style_url(self) -> None:
        self.assertEqual(
            gitrepos.owner_from_remote_url("git@github.com:cairnea/KinDash.git"),
            "cairnea",
        )

    def test_non_github_remote_has_no_owner(self) -> None:
        # A local path or self-hosted remote has no App installation behind it.
        self.assertIsNone(gitrepos.owner_from_remote_url("/tmp/fixtures/remote.git"))
        self.assertIsNone(
            gitrepos.owner_from_remote_url("https://gitlab.com/acme/widget.git")
        )

    def test_empty_url(self) -> None:
        self.assertIsNone(gitrepos.owner_from_remote_url(""))


class GitEnvForRemoteTests(unittest.TestCase):
    """The single entry point other modules use for credentialed git env."""

    def tearDown(self) -> None:
        gitrepos.set_token_provider(None)

    def test_returns_none_without_a_provider(self) -> None:
        gitrepos.set_token_provider(None)
        self.assertIsNone(
            gitrepos.git_env_for_remote("https://github.com/cairnea/KinDash.git")
        )

    def test_builds_credential_env_from_the_url_owner(self) -> None:
        seen: list[str] = []

        def provider(owner: str) -> str | None:
            seen.append(owner)
            return "ghs_tok"

        gitrepos.set_token_provider(provider)
        env = gitrepos.git_env_for_remote("https://github.com/cairnea/KinDash.git")
        assert env is not None
        self.assertEqual(seen, ["cairnea"])
        self.assertEqual(env["CCSWITCH_GIT_TOKEN"], "ghs_tok")

    def test_non_github_remote_gets_ambient_env(self) -> None:
        gitrepos.set_token_provider(lambda owner: "ghs_tok")
        self.assertIsNone(gitrepos.git_env_for_remote("/tmp/fixtures/remote.git"))


class GitTokenPlumbingTests(unittest.TestCase):
    """``ensure_clone`` passes the repo owner to the network git calls only."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "GitHub"
        self.root.mkdir()
        self.remote = self.base / "remote.git"
        _make_remote(self.remote)
        self.addCleanup(gitrepos.set_token_provider, None)

    def test_clone_and_fetch_receive_the_owner(self) -> None:
        real = gitrepos._run_git
        calls: list[tuple[list[str], str | None]] = []

        def spy(args, owner=None):
            calls.append((list(args), owner))
            return real(args, owner=owner)

        with mock.patch.object(gitrepos, "_run_git", spy):
            gitrepos.ensure_clone(str(self.root), "acme/widget", str(self.remote))
            gitrepos.ensure_clone(str(self.root), "acme/widget", str(self.remote))

        clones = [c for c in calls if c[0] and c[0][0] == "clone"]
        fetches = [c for c in calls if "fetch" in c[0]]
        self.assertTrue(clones and fetches)
        for _args, owner in clones + fetches:
            self.assertEqual(owner, "acme")

    def test_local_git_calls_do_not_get_a_token(self) -> None:
        # `checkout --detach` and `remote get-url` touch no network; handing them
        # a credential would widen the token's blast radius for no reason.
        real = gitrepos._run_git
        calls: list[tuple[list[str], str | None]] = []

        def spy(args, owner=None):
            calls.append((list(args), owner))
            return real(args, owner=owner)

        with mock.patch.object(gitrepos, "_run_git", spy):
            gitrepos.ensure_clone(str(self.root), "acme/widget", str(self.remote))

        local = [c for c in calls if "checkout" in c[0] or "get-url" in c[0]]
        self.assertTrue(local, "expected at least one local git call")
        for _args, owner in local:
            self.assertIsNone(owner)

    def test_token_never_appears_in_argv(self) -> None:
        gitrepos.set_token_provider(lambda owner: "ghs_supersecret")
        real = gitrepos._run_git
        seen: list[str] = []

        def spy(args, owner=None):
            seen.extend(args)
            return real(args, owner=owner)

        with mock.patch.object(gitrepos, "_run_git", spy):
            gitrepos.ensure_clone(str(self.root), "acme/widget", str(self.remote))

        for arg in seen:
            self.assertNotIn("ghs_supersecret", arg)

    def test_token_never_written_to_the_clone_config(self) -> None:
        gitrepos.set_token_provider(lambda owner: "ghs_supersecret")
        dest = gitrepos.ensure_clone(str(self.root), "acme/widget", str(self.remote))
        config = (Path(dest) / ".git" / "config").read_text()
        self.assertNotIn("ghs_supersecret", config)


class TokenSettingsUrlTests(unittest.TestCase):
    """The banner's "fix this" link must name the credential that actually failed."""

    def test_defaults_to_the_pat_page(self) -> None:
        # No App in play: the failing credential really is a PAT/gh login.
        self.assertEqual(
            gitrepos.token_settings_url("acme/widget"),
            "https://github.com/settings/tokens",
        )

    def test_app_backed_failure_links_to_installations(self) -> None:
        # An App-token fetch failed, so refreshing a PAT fixes nothing — the
        # actionable page is the App installation.
        self.assertEqual(
            gitrepos.token_settings_url("acme/widget", via_app=True),
            "https://github.com/settings/installations",
        )

    def test_registry_reports_the_app_url_for_an_app_backed_failure(self) -> None:
        gitrepos.clear_stale_credentials("acme/widget")
        self.addCleanup(gitrepos.clear_stale_credentials, "acme/widget")
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="remote: Forbidden", via_app=True
        )
        rows = [r for r in gitrepos.stale_credential_repos() if r["repo"] == "acme/widget"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["settingsUrl"], "https://github.com/settings/installations"
        )

    def test_registry_exposes_via_app_for_the_banner(self) -> None:
        # The banner's wording/label depends on which credential failed. The
        # server decides it; the client must not re-derive it from the URL.
        gitrepos.clear_stale_credentials("acme/widget")
        self.addCleanup(gitrepos.clear_stale_credentials, "acme/widget")
        gitrepos.record_fetch_result(
            "acme/widget", ok=False, stderr="remote: Forbidden", via_app=True
        )
        row = next(
            r for r in gitrepos.stale_credential_repos() if r["repo"] == "acme/widget"
        )
        self.assertIs(row["viaApp"], True)

    def test_registry_via_app_defaults_false(self) -> None:
        gitrepos.clear_stale_credentials("acme/plain")
        self.addCleanup(gitrepos.clear_stale_credentials, "acme/plain")
        gitrepos.record_fetch_result(
            "acme/plain", ok=False, stderr="Authentication failed")
        row = next(
            r for r in gitrepos.stale_credential_repos() if r["repo"] == "acme/plain"
        )
        self.assertIs(row["viaApp"], False)
        self.assertEqual(row["settingsUrl"], "https://github.com/settings/tokens")


# ---------------------------------------------------------------------------
# App-installation-backed repo listing (issue #912)
# ---------------------------------------------------------------------------


def _inst_repo(
    full_name: str,
    *,
    pushed_at: str,
    archived: bool = False,
    default_branch: str | None = "main",
    private: bool = True,
) -> dict:
    """One element of a REST ``/installation/repositories`` response."""
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "private": private,
        "html_url": f"https://github.com/{full_name}",
        "default_branch": default_branch,
        "pushed_at": pushed_at,
        "archived": archived,
    }


def _jsonl(*repos: dict) -> str:
    """What ``gh api --paginate --jq '.repositories[]'`` writes: one repo per line."""
    return "\n".join(json.dumps(r) for r in repos) + "\n"


class InstallationListingTests(unittest.TestCase):
    """``list_accessible_repos`` sources from the GitHub App installations.

    The old ``viewer.repositories`` query authenticates as the *user*; an App
    installation token has no viewer at all, so the App path must enumerate
    ``/installation/repositories`` per installation instead.
    """

    def tearDown(self) -> None:
        gitrepos.set_token_provider(None)
        gitrepos.set_installations_provider(None)

    def _register(self, owners: list[str]) -> None:
        gitrepos.set_installations_provider(lambda: owners)
        gitrepos.set_token_provider(lambda owner: f"ghs_{owner}")

    def test_lists_installation_repos_with_that_owners_app_token(self) -> None:
        self._register(["steiner385"])
        calls: list[dict] = []

        def fake_run(args, capture_output, text, env, input=None):
            calls.append({"args": args, "env": env})
            return subprocess.CompletedProcess(
                args, 0,
                stdout=_jsonl(_inst_repo("steiner385/ccswitch",
                                         pushed_at="2026-09-01T00:00:00Z")),
                stderr="",
            )

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        self.assertIn("/installation/repositories", " ".join(calls[0]["args"]))
        # The installation token, not the machine's ambient gh credential.
        self.assertEqual(calls[0]["env"]["GH_TOKEN"], "ghs_steiner385")
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["nameWithOwner"], "steiner385/ccswitch")
        self.assertEqual(repos[0]["owner"], "steiner385")
        self.assertEqual(repos[0]["defaultBranch"], "main")
        self.assertEqual(repos[0]["pushedAt"], "2026-09-01T00:00:00Z")
        self.assertTrue(repos[0]["isPrivate"])

    def test_never_issues_a_viewer_query_on_the_app_path(self) -> None:
        # A viewer.* query would authenticate as the user, which is exactly the
        # dependency this change removes.
        self._register(["steiner385"])

        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(
                args, 0,
                stdout=_jsonl(_inst_repo("steiner385/a", pushed_at="2026-01-01T00:00:00Z")),
                stderr="",
            )

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run) as run:
            gitrepos.list_accessible_repos()

        for call in run.call_args_list:
            self.assertNotIn("viewer", " ".join(call.args[0]))

    def test_merges_installations_deduped_and_pushed_at_desc(self) -> None:
        self._register(["steiner385", "cairnea"])
        by_owner = {
            "ghs_steiner385": _jsonl(
                _inst_repo("steiner385/old", pushed_at="2026-01-01T00:00:00Z"),
                _inst_repo("shared/repo", pushed_at="2026-08-01T00:00:00Z"),
            ),
            "ghs_cairnea": _jsonl(
                _inst_repo("cairnea/KinDash", pushed_at="2026-09-01T00:00:00Z"),
                _inst_repo("shared/repo", pushed_at="2026-08-01T00:00:00Z"),
            ),
        }

        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(
                args, 0, stdout=by_owner[env["GH_TOKEN"]], stderr="")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        self.assertEqual(
            [r["nameWithOwner"] for r in repos],
            ["cairnea/KinDash", "shared/repo", "steiner385/old"],
        )

    def test_excludes_archived_repos(self) -> None:
        self._register(["steiner385"])

        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(
                args, 0,
                stdout=_jsonl(
                    _inst_repo("steiner385/live", pushed_at="2026-09-01T00:00:00Z"),
                    _inst_repo("steiner385/dead", pushed_at="2026-09-02T00:00:00Z",
                               archived=True),
                ),
                stderr="",
            )

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        self.assertEqual([r["nameWithOwner"] for r in repos], ["steiner385/live"])

    def test_all_installations_failing_raises_actionable_error(self) -> None:
        # Fail loudly: silently degrading to the user's gh token is what made a
        # dead credential look like an empty repo list.
        self._register(["steiner385", "cairnea"])

        def fake_run(args, capture_output, text, env, input=None):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: Bad credentials")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            with self.assertRaises(gitrepos.GhError) as ctx:
                gitrepos.list_accessible_repos()

        self.assertIn("installation", str(ctx.exception).lower())

    def test_one_failing_installation_does_not_blank_the_chooser(self) -> None:
        self._register(["steiner385", "cairnea"])

        def fake_run(args, capture_output, text, env, input=None):
            if env["GH_TOKEN"] == "ghs_cairnea":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: boom")
            return subprocess.CompletedProcess(
                args, 0,
                stdout=_jsonl(_inst_repo("steiner385/ccswitch",
                                         pushed_at="2026-09-01T00:00:00Z")),
                stderr="",
            )

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        self.assertEqual([r["nameWithOwner"] for r in repos], ["steiner385/ccswitch"])

    def test_provider_registered_but_no_installations_raises(self) -> None:
        gitrepos.set_installations_provider(lambda: [])
        gitrepos.set_token_provider(lambda owner: "unused")

        with mock.patch("ccswitch.gitrepos.subprocess.run") as run:
            with self.assertRaises(gitrepos.GhError) as ctx:
                gitrepos.list_accessible_repos()

        run.assert_not_called()
        self.assertIn("installation", str(ctx.exception).lower())

    def test_owner_without_a_token_is_skipped_not_run_unauthenticated(self) -> None:
        # A missing token must never fall through to an unauthenticated gh call —
        # that is what exhausted the 60-req/hr anonymous quota.
        gitrepos.set_installations_provider(lambda: ["steiner385"])
        gitrepos.set_token_provider(lambda owner: None)

        with mock.patch("ccswitch.gitrepos.subprocess.run") as run:
            with self.assertRaises(gitrepos.GhError):
                gitrepos.list_accessible_repos()

        run.assert_not_called()

    def test_no_provider_registered_keeps_the_viewer_path(self) -> None:
        # Standalone CLI (no daemon): nothing is registered, so the user's own gh
        # credential still backs the listing.
        gitrepos.set_installations_provider(None)
        gitrepos.set_token_provider(None)
        seen: dict = {}

        def fake_run(args, capture_output, text, env, input=None):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout=_REPOS_JSON, stderr="")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            repos = gitrepos.list_accessible_repos()

        self.assertIn("viewer", " ".join(seen["args"]))
        self.assertEqual(repos[0]["nameWithOwner"], "cairnea/KinDash")


class BranchListingAuthTests(unittest.TestCase):
    """Branch/default-branch lookups authenticate as the App when one is registered."""

    def tearDown(self) -> None:
        gitrepos.set_token_provider(None)
        gitrepos.set_installations_provider(None)

    def test_list_remote_branches_uses_the_owners_app_token(self) -> None:
        gitrepos.set_token_provider(lambda owner: f"ghs_{owner}")
        envs: list[dict] = []

        def fake_run(args, capture_output, text, env, input=None):
            envs.append(env)
            if "graphql" in args:
                payload = {
                    "data": {
                        "repository": {
                            "refs": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"name": "main"}],
                            }
                        }
                    }
                }
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload),
                                                   stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

        with mock.patch("ccswitch.gitrepos.subprocess.run", side_effect=fake_run):
            gitrepos.list_remote_branches("steiner385/ccswitch")

        self.assertTrue(envs, "expected at least one gh invocation")
        for env in envs:
            self.assertEqual(env.get("GH_TOKEN"), "ghs_steiner385")


if __name__ == "__main__":
    unittest.main()
