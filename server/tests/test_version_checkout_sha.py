import subprocess
from server import version


def test_checkout_sha_is_fresh_after_ttl_and_cached_within_it():
    version._reset_checkout_cache_for_tests()
    t = [100.0]
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"sha{len(calls)}\n", stderr="")
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha1"
    t[0] += 5.0
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha1", "cached within 10 s"
    t[0] += 6.0
    assert version.checkout_git_sha(now=lambda: t[0], run=run) == "sha2", "re-read after the TTL"
    assert calls[0][:3] == ["git", "rev-parse", "--short"]


def test_checkout_sha_unknown_on_git_failure():
    version._reset_checkout_cache_for_tests()
    def boom(argv, **kw):
        raise OSError("no git")
    assert version.checkout_git_sha(now=lambda: 0.0, run=boom) == "unknown"


def test_repo_root_is_the_checkout_containing_server_package():
    import os
    assert os.path.isdir(os.path.join(version.repo_root(), "server"))
