from server.github.pr_status import PullRequestInfo, CheckDetail
from server.github.parity.normalize import OraclePr
from server.github.parity.harness import diff_snapshots


def _cand(**kw):
    base = dict(number=1, url="u", state="open", checks="passing", title="t",
                is_draft=False, merged=False, head_sha="abc",
                checks_detail=(CheckDetail("ci", "success"),))
    base.update(kw)
    return PullRequestInfo(**base)


def _oracle(**kw):
    fields = dict(number=1, url="u", state="open", checks="passing",
                  is_draft=False, merged=False, head_sha="abc",
                  checks_detail=[{"name": "ci", "conclusion": "success"}])
    fields.update(kw)
    return OraclePr(repo="o/r", number=fields["number"], branch="br", fields=fields)


def test_identical_snapshots_match():
    rep = diff_snapshots({("o/r", 1): _cand()}, {("o/r", 1): _oracle()}, phase=0)
    assert rep.ok and rep.matched == 1 and not rep.mismatches


def test_field_mismatch_detected():
    rep = diff_snapshots({("o/r", 1): _cand(state="open")},
                         {("o/r", 1): _oracle(state="merged")}, phase=0)
    assert not rep.ok
    assert any(m.field == "state" for m in rep.mismatches)


def test_uncovered_field_ignored():
    # 'title' is NOT in PHASE_FIELDS[0]; a difference there must NOT fail parity.
    rep = diff_snapshots({("o/r", 1): _cand(title="A")},
                         {("o/r", 1): _oracle()}, phase=0)  # oracle has no title
    assert rep.ok


def test_oracle_only_and_candidate_only():
    rep = diff_snapshots({("o/r", 1): _cand()},
                         {("o/r", 2): _oracle(number=2)}, phase=0)
    assert ("o/r", 1) in rep.candidate_only
    assert ("o/r", 2) in rep.oracle_only
    assert not rep.ok
