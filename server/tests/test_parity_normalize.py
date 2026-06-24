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
