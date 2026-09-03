from server.deploy.state import DeployState
from server.sessions.version_skew import detect_skew

S = DeployState(deployed_sha="b" * 40, deployed_short="bbbbbbb", daemon_sha="a" * 40, deployed_at=None)
ANCESTORS = {("a" * 40, "bbbbbbb"), ("a" * 40, "aaaaaaa"), ("a" * 40, "ccccccc")}
def anc(a, d): return (a, d) in ANCESTORS


def test_no_state_means_no_gate():
    assert detect_skew(checkout_sha="zzz", process_sha="zzz", state=None, is_ancestor=anc) is None


def test_in_sync_after_daemon_changing_deploy():
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="bbbbbbb", state=S, is_ancestor=anc) is None


def test_checkout_behind_is_skew():
    err = detect_skew(checkout_sha="aaaaaaa", process_sha="aaaaaaa", state=S, is_ancestor=anc)
    assert err and "version skew" in err and "checkout=aaaaaaa" in err and "deployed=bbbbbbb" in err


def test_process_older_than_daemon_sha_is_skew_even_with_current_checkout():
    # checkout was fast-forwarded (edge-only path) but the process predates daemon_sha
    ANCESTORS.discard(("a" * 40, "0000000"))
    err = detect_skew(checkout_sha="bbbbbbb", process_sha="0000000", state=S, is_ancestor=anc)
    assert err and "process=0000000" in err


def test_process_descendant_of_daemon_sha_is_fine():
    # process started at ccccccc (a web-only commit after daemon_sha) — current
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="ccccccc", state=S, is_ancestor=anc) is None


def test_missing_checkout_sha_falls_back_to_process_sha():
    assert detect_skew(checkout_sha=None, process_sha="bbbbbbb", state=S, is_ancestor=anc) is None
    assert detect_skew(checkout_sha=None, process_sha="aaaaaaa", state=S, is_ancestor=anc) is not None


def test_no_daemon_sha_skips_process_check():
    s = DeployState(deployed_sha="b" * 40, deployed_short="bbbbbbb", daemon_sha=None, deployed_at=None)
    assert detect_skew(checkout_sha="bbbbbbb", process_sha="0000000", state=s, is_ancestor=anc) is None


def test_prefix_abbreviations_match():
    assert detect_skew(checkout_sha="bbbbbbbb1", process_sha="bbbbbb", state=S,
                       is_ancestor=lambda a, d: True) is None
