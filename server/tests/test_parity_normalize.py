import json
from pathlib import Path

from server.github.parity.normalize import normalize_state, OraclePr

FIXTURE = Path(__file__).parent / "fixtures" / "plotroom_state_sample.json"


def test_normalize_keys_by_repo_and_number():
    state = json.loads(FIXTURE.read_text())
    out = normalize_state(state)
    assert out, "fixture should contain at least one PR"
    (repo, number), pr = next(iter(out.items()))
    assert isinstance(pr, OraclePr)
    assert pr.fields["number"] == number
    assert "state" in pr.fields and "checks" in pr.fields


def test_normalize_maps_state_vocabulary():
    # Plotroom's per-PR shape -> ccswitch vocabulary ('open'/'draft'/'merged'/'closed').
    sample = {"repos": [{"nameWithOwner": "o/r", "prs": [
        {"number": 1, "url": "u", "headRefName": "br", "headSha": "abc",
         "state": "OPEN", "isDraft": True, "checksRollup": "PENDING", "checks": []},
    ]}]}
    out = normalize_state(sample)
    pr = out[("o/r", 1)]
    assert pr.fields["state"] == "draft"     # isDraft wins over OPEN
    assert pr.fields["checks"] == "pending"
    assert pr.branch == "br" and pr.fields["head_sha"] == "abc"


def test_normalize_emits_stage_for_open_stages():
    """Oracle stage is extracted for open stages (ci/ready/parked) and merged."""
    def _pr_with_stage(number: int, stage_str: str) -> dict:
        return {"number": number, "url": f"u{number}", "checks": [],
                "stage": {"stage": stage_str}}

    sample = {"repos": [{"repo": "o/r", "prs": [
        _pr_with_stage(1, "ready"),
        _pr_with_stage(2, "ci"),
        _pr_with_stage(3, "parked"),
        _pr_with_stage(4, "queue"),    # not covered → no stage field
        _pr_with_stage(5, "merged"),   # covered → stage="merged"
    ]}]}
    out = normalize_state(sample)
    assert out[("o/r", 1)].fields["stage"] == "ready"
    assert out[("o/r", 2)].fields["stage"] == "ci"
    assert out[("o/r", 3)].fields["stage"] == "parked"
    assert "stage" not in out[("o/r", 4)].fields
    assert out[("o/r", 5)].fields["stage"] == "merged"


def test_normalize_open_pr_has_checks():
    """Open PRs emit checks into fields."""
    sample = {"repos": [{"repo": "o/r", "prs": [
        {"number": 1, "url": "u1", "checks": [], "stage": {"stage": "ready"}},
    ]}]}
    out = normalize_state(sample)
    assert "checks" in out[("o/r", 1)].fields


def test_normalize_merged_pr_drops_checks():
    """Merged PRs do NOT emit checks — frozen rollup causes false mismatches."""
    sample = {"repos": [{"repo": "o/r", "prs": [
        {"number": 42, "url": "u42", "checks": [], "stage": {"stage": "merged"}},
    ]}]}
    out = normalize_state(sample)
    assert "checks" not in out[("o/r", 42)].fields
    assert "checks_detail" not in out[("o/r", 42)].fields
    assert out[("o/r", 42)].fields["stage"] == "merged"
