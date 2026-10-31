"""Pure systemic-red evaluator: rule (C)/(M), clearing, classification, brake marker."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from server.automations.cicd.systemic import (
    BRAKE_CLASSES,
    Hit,
    JobObs,
    RunObs,
    brake_marker,
    classify,
    detect,
    fingerprint,
    fix_prompt,
    is_cleared,
    issue_body,
    normalize_job,
    poisoner_pr,
    pr_of_branch,
    signature,
    strip_brake_marker,
    thrash,
)

MIN = 60.0
WINDOW = 60 * MIN
ROLLUPS = frozenset({"ci", "ci-gate"})
FEED_RE = "^security: audit$"
WF = 100
MAIN_SHA = "a1a1a1a1"
T0 = 1_000_000.0
FIXTURE = Path(__file__).parent / "fixtures" / "cicd_systemic_2026_10_31.json"

_run_ids = iter(range(1, 10_000))
_job_ids = iter(range(50_000, 90_000))


def _job(name: str, conclusion: str, done: float | None, status: str = "completed") -> JobObs:
    return JobObs(name=name, conclusion=conclusion if status == "completed" else "",
                  status=status, completed_ts=done, job_id=next(_job_ids))


def _cand(pr: int, created: float, jobs: list[JobObs], *, base: str = MAIN_SHA,
          head: str | None = None, status: str = "completed",
          conclusion: str = "failure") -> RunObs:
    rid = next(_run_ids)
    return RunObs(run_id=rid, workflow_id=WF, event="merge_group",
                  head_branch=f"gh-readonly-queue/main/pr-{pr}-{base}",
                  head_sha=head or f"c0ffee{rid:04x}", created_ts=created, status=status,
                  conclusion=conclusion, jobs=tuple(jobs))


def _main(created: float, jobs: list[JobObs], head: str = MAIN_SHA) -> RunObs:
    return RunObs(run_id=next(_run_ids), workflow_id=WF, event="push", head_branch="main",
                  head_sha=head, created_ts=created, status="completed",
                  conclusion="success", jobs=tuple(jobs))


def _fail(pr: int, created: float, job: str = "static-checks / security: audit", **kw) -> RunObs:
    return _cand(pr, created, [_job(job, "failure", created + 5 * MIN),
                               _job("ci", "failure", created + 6 * MIN)], **kw)


def _green(pr: int, created: float, job: str = "static-checks / security: audit", **kw) -> RunObs:
    return _cand(pr, created, [_job(job, "success", created + 5 * MIN)],
                 conclusion="success", **kw)


def _detect(cands, mains=None, now=T0 + 50 * MIN):
    return detect(cands, mains or {}, now=now, window_s=WINDOW, min_prs=3, rollups=ROLLUPS)


# --- parsing ---------------------------------------------------------------


def test_normalize_job_strips_reusable_prefix():
    assert normalize_job("static-checks / security: audit") == "security: audit"
    assert normalize_job("a / b / test: unit (3/4)") == "test: unit (3/4)"
    assert normalize_job("ci-gate") == "ci-gate"


def test_pr_of_branch_parses_queue_branch_and_rejects_others():
    assert pr_of_branch("gh-readonly-queue/main/pr-20310-3fb396c0") == (20310, "3fb396c0")
    assert pr_of_branch("gh-readonly-queue/release/v2/pr-7-abc1234") == (7, "abc1234")
    assert pr_of_branch("main") is None
    assert pr_of_branch("feat/pr-1-abc") is None


# --- rule (C) / (M) ----------------------------------------------------------


def test_three_distinct_prs_failing_one_job_is_rule_c():
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN)]
    hits = _detect(cands)
    assert len(hits) == 1
    h = hits[0]
    assert (h.job, h.rule, h.prs) == ("security: audit", "C", (1, 2, 3))
    assert h.first_failed_ts == T0 + 5 * MIN
    assert h.last_failed_ts == T0 + 25 * MIN
    assert h.failing_runs == tuple(c.run_id for c in cands)
    assert len(h.failing_job_ids) == 3


def test_same_pr_rebuilt_three_times_is_not_systemic():
    cands = [_fail(1, T0), _fail(1, T0 + 10 * MIN), _fail(1, T0 + 20 * MIN),
             _fail(2, T0 + 25 * MIN)]
    assert _detect(cands) == []


def test_rollup_jobs_never_hit():
    cands = [_cand(pr, T0 + pr * MIN, [_job("ci", "failure", T0 + 30 * MIN),
                                       _job("ci-gate", "failure", T0 + 31 * MIN)])
             for pr in range(1, 6)]
    assert _detect(cands) == []


def test_later_green_candidate_for_the_job_prevents_hit():
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN),
             _green(4, T0 + 30 * MIN)]
    assert _detect(cands) == []
    # A green candidate created BEFORE the earliest failure proves nothing.
    early = [_green(4, T0 - 5 * MIN)] + cands[:3]
    assert [h.job for h in _detect(early)] == ["security: audit"]


def test_in_progress_run_counts_only_completed_failed_jobs():
    now = T0 + 40 * MIN
    third = _cand(3, T0 + 20 * MIN, [
        _job("static-checks / security: audit", "failure", T0 + 25 * MIN),
        _job("e2e: shard 1", "", None, status="in_progress"),
    ], status="in_progress", conclusion="")
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), third]
    assert [h.prs for h in _detect(cands, now=now)] == [(1, 2, 3)]
    # A job not yet completed (or completing after `now`) is not evidence.
    pending = _cand(3, T0 + 20 * MIN, [
        _job("static-checks / security: audit", "", None, status="in_progress")],
        status="in_progress", conclusion="")
    assert _detect([_fail(1, T0), _fail(2, T0 + 10 * MIN), pending], now=now) == []
    assert _detect(cands, now=T0 + 24 * MIN) == []


def test_main_failure_plus_one_later_candidate_is_rule_m():
    main = _main(T0, [_job("docs: conformance", "failure", T0 + 5 * MIN)])
    cand = _fail(7, T0 + 10 * MIN, job="docs: conformance")
    hits = _detect([cand], {WF: main})
    assert [(h.job, h.rule, h.prs) for h in hits] == [("docs: conformance", "M", (7,))]
    # A candidate created before the main run does not count toward (M).
    older = _fail(7, T0 - 10 * MIN, job="docs: conformance")
    assert _detect([older], {WF: main}, now=T0 + 40 * MIN) == []


def test_window_excludes_old_candidates():
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN)]
    assert _detect(cands, now=T0 + 20 * MIN + WINDOW + 1) == []


# --- replay of the real 2026-10-31 incident ----------------------------------


def _ts(iso: str | None) -> float | None:
    return None if iso is None else datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _run_from(d: dict) -> RunObs:
    created = _ts(d["created_at"])
    assert created is not None
    return RunObs(run_id=d["run_id"], workflow_id=d["workflow_id"], event=d["event"],
                  head_branch=d["head_branch"], head_sha=d["head_sha"], created_ts=created,
                  status=d["status"], conclusion=d["conclusion"] or "",
                  jobs=tuple(JobObs(name=j["name"], conclusion=j["conclusion"] or "",
                                    status=j["status"], completed_ts=_ts(j["completed_at"]),
                                    job_id=j["id"]) for j in d["jobs"]))


def test_replay_2026_10_31_opens_on_security_audit_by_0110_utc():
    data = json.loads(FIXTURE.read_text())
    cands = [_run_from(c) for c in data["candidates"]]
    main = _run_from(data["main"])
    start, end = (_ts(t) for t in data["window"])
    assert start is not None and end is not None
    now = start
    opened: tuple[float, Hit] | None = None
    while now <= end and opened is None:
        mains = {main.workflow_id: main} if main.created_ts <= now else {}
        visible = [c for c in cands if c.created_ts <= now]
        for h in detect(visible, mains, now=now, window_s=WINDOW, min_prs=3, rollups=ROLLUPS):
            if h.job == "security: audit":
                opened = (now, h)
        now += 120.0
    assert opened is not None
    at, hit = opened
    # Third distinct-PR failure completed 01:01:36Z; the next 120 s tick is 01:02:00Z.
    assert at <= _ts("2026-10-31T01:10:00Z")  # type: ignore[operator]
    assert at == _ts("2026-10-31T01:02:00Z")
    assert hit.rule == "C" and hit.prs == (20234, 20304, 20305)
    visible = [c for c in cands if c.created_ts <= at]
    assert classify(hit, visible, {main.workflow_id: main},
                    normalized_sig="Found N vulnerabilities", external_feed_re=FEED_RE) \
        == "external_feed"
    # The incident does not clear: no candidate ever ran the audit green again.
    assert not is_cleared("security: audit", at, cands, {main.workflow_id: main},
                          now=_ts("2026-10-31T11:30:00Z"), quiet_s=90 * MIN,  # type: ignore[arg-type]
                          rollups=ROLLUPS)


def test_replay_2026_10_31_without_feed_regex_classifies_poisoner():
    """Locks why external_feed is checked first: on 10-31 the serial queue made every
    failing stack share the front PR and push:main ran the audit green, so without the
    external-feed rule the classifier would call it a poisoner (no brake, no fix)."""
    data = json.loads(FIXTURE.read_text())
    cands = [_run_from(c) for c in data["candidates"]]
    main = _run_from(data["main"])
    at = _ts("2026-10-31T01:02:00Z")
    assert at is not None
    visible = [c for c in cands if c.created_ts <= at]
    mains = {main.workflow_id: main}
    hit = next(h for h in detect(visible, mains, now=at, window_s=WINDOW, min_prs=3,
                                 rollups=ROLLUPS) if h.job == "security: audit")
    assert classify(hit, visible, mains, normalized_sig="Found N vulnerabilities",
                    external_feed_re="^$x") == "poisoner"
    assert poisoner_pr(hit, visible, mains) in hit.prs


# --- classification ------------------------------------------------------------


def _chain(prs: list[int], job: str, *, failing: bool = True, start: float = T0,
           base: str = MAIN_SHA) -> list[RunObs]:
    """A serial queue: each candidate's base is the previous candidate's head."""
    out: list[RunObs] = []
    for i, pr in enumerate(prs):
        mk = _fail if failing else _green
        c = mk(pr, start + i * MIN, job=job, base=base)
        base = c.head_sha
        out.append(c)
    return out


def _hit_for(cands, mains, job):
    hits = [h for h in _detect(cands, mains, now=T0 + 30 * MIN) if h.job == job]
    assert hits, "expected a hit"
    return hits[0]


def test_classify_external_feed_wins_over_shared_stack():
    main = _main(T0 - 30 * MIN, [_job("static-checks / security: audit", "success", T0 - 25 * MIN)])
    cands = _chain([9, 10, 11, 12], "static-checks / security: audit")
    hit = _hit_for(cands, {WF: main}, "security: audit")
    assert classify(hit, cands, {WF: main}, normalized_sig="", external_feed_re=FEED_RE) \
        == "external_feed"
    # An advisory id in the signature is external_feed for any job name.
    other = _chain([9, 10, 11], "docs: conformance")
    h2 = _hit_for(other, {}, "docs: conformance")
    assert classify(h2, other, {}, normalized_sig="CVE-2026-12345 in lodash",
                    external_feed_re=FEED_RE) == "external_feed"


def test_classify_poisoner_when_only_stacks_with_p_fail_and_main_green():
    job = "docs: conformance"
    main = _main(T0 - 30 * MIN, [_job(job, "success", T0 - 25 * MIN)])
    cands = _chain([9, 10, 11, 12], job)
    # A candidate whose stack lacks PR 9 (based directly on main) ran the job green.
    clean = _green(13, T0 - 10 * MIN, job=job)
    hit = _hit_for(cands + [clean], {WF: main}, job)
    klass = classify(hit, cands + [clean], {WF: main}, normalized_sig="", external_feed_re=FEED_RE)
    assert klass == "poisoner"
    assert klass not in BRAKE_CLASSES


def test_classify_infra_on_startup_failure_or_runner_signature():
    job = "test: bats"
    cands = [_fail(1, T0, job=job), _fail(2, T0 + MIN, job=job), _fail(3, T0 + 2 * MIN, job=job)]
    hit = _hit_for(cands, {}, job)
    sig, _ = signature("##[error]The runner has received a shutdown signal. ...")
    assert classify(hit, cands, {}, normalized_sig=sig, external_feed_re=FEED_RE) == "infra"
    sig137, _ = signature("##[error]Process completed with exit code 137.")
    assert classify(hit, cands, {}, normalized_sig=sig137, external_feed_re=FEED_RE) == "infra"
    startup = [_cand(pr, T0 + pr * MIN, [_job(job, "startup_failure", T0 + 5 * MIN)])
               for pr in (1, 2, 3)]
    h2 = _hit_for(startup, {}, job)
    assert classify(h2, startup, {}, normalized_sig="", external_feed_re=FEED_RE) == "infra"


def test_classify_main_regression_on_rule_m():
    job = "docs: conformance"
    main = _main(T0, [_job(job, "failure", T0 + 5 * MIN)])
    cand = _fail(7, T0 + 10 * MIN, job=job)
    hit = _hit_for([cand], {WF: main}, job)
    assert hit.rule == "M"
    klass = classify(hit, [cand], {WF: main}, normalized_sig="", external_feed_re=FEED_RE)
    assert klass == "main_regression" and klass in BRAKE_CLASSES
    # (C) with complete stacks that share no PR is also a main regression.
    lone = [_fail(1, T0, job=job), _fail(2, T0 + MIN, job=job), _fail(3, T0 + 2 * MIN, job=job)]
    main_green = _main(T0 - 30 * MIN, [_job(job, "success", T0 - 25 * MIN)])
    h2 = _hit_for(lone, {WF: main_green}, job)
    assert classify(h2, lone, {WF: main_green}, normalized_sig="",
                    external_feed_re=FEED_RE) == "main_regression"


def test_classify_ambiguous_stack_is_unknown_not_a_brake_class():
    job = "docs: conformance"
    main = _main(T0 - 30 * MIN, [_job(job, "success", T0 - 25 * MIN)])
    # Bases point at shas no observed candidate (nor main) produced: partial stacks.
    cands = [_fail(pr, T0 + pr * MIN, job=job, base=f"deadbee{pr}") for pr in (1, 2, 3)]
    hit = _hit_for(cands, {WF: main}, job)
    klass = classify(hit, cands, {WF: main}, normalized_sig="", external_feed_re=FEED_RE)
    assert klass == "unknown"
    assert klass not in BRAKE_CLASSES


# --- signature / fingerprint -----------------------------------------------------


def test_signature_prefers_first_error_line_and_normalizes():
    log = "\n".join([
        "2026-10-31T00:59:58.1234567Z Run pnpm audit",
        "2026-10-31T00:59:59.1234567Z some failed noise",
        "2026-10-31T01:00:00.1234567Z ##[error]Found 10 vulnerabilities (GHSA-abcd-1234-wxyz) "
        "at 2026-10-31T01:00:00Z sha 3fb396c0aa",
        "2026-10-31T01:00:01.1234567Z ##[error]Process completed with exit code 1.",
    ])
    norm, raw = signature(log)
    assert "N" in norm
    assert "GHSA-abcd-1234-wxyz" in norm
    assert "2026" not in norm and "01:00" not in norm
    assert "3fb396c0aa" not in norm
    assert norm.startswith("Found N vulnerabilities")
    assert "exit code" not in norm
    assert "Found 10 vulnerabilities" in raw
    assert len(raw.splitlines()) <= 40


def test_signature_falls_back_to_error_like_line_and_strips_node_modules_paths():
    lines = [f"line {i}" for i in range(100)]
    lines[70] = "Error: Cannot find module node_modules/.pnpm/foo@1.2.3/node_modules/foo/index.js"
    norm, raw = signature("\n".join(lines))
    assert norm.startswith("Error: Cannot find module node_modules/")
    assert "foo@" not in norm
    assert len(raw.splitlines()) <= 40
    assert signature("") == ("", "")


def test_fingerprint_is_stable_and_repo_scoped():
    a = fingerprint("cairnea/KinDash", "security: audit", "Found N vulnerabilities")
    assert a == fingerprint("cairnea/KinDash", "security: audit", "Found N vulnerabilities")
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    assert a != fingerprint("cairnea/Other", "security: audit", "Found N vulnerabilities")
    assert a != fingerprint("cairnea/KinDash", "docs: conformance", "Found N vulnerabilities")
    assert a != fingerprint("cairnea/KinDash", "security: audit", "other")


# --- brake marker -------------------------------------------------------------------


def test_brake_marker_round_trip_matches_kindash_contract():
    set_at = _ts("2026-10-31T01:01:00Z")
    assert set_at is not None
    marker = brake_marker(job="security: audit", klass="external_feed", incident="abc123def456",
                          set_at=set_at, expires_at=set_at + 4 * 3600)
    m = re.search(r"<!-- viberpit:systemic-brake (\{.*?\}) -->", marker)
    assert m
    payload = json.loads(m.group(1))
    assert set(payload) == {"job", "class", "incident", "set_at", "expires_at", "by"}
    assert payload["job"] == "security: audit"
    assert payload["class"] == "external_feed"
    assert payload["incident"] == "abc123def456"
    assert payload["by"] == "viberpit-cicd-doctor"
    assert payload["set_at"] == "2026-10-31T01:01:00Z"
    assert payload["expires_at"] == "2026-10-31T05:01:00Z"
    for k in ("set_at", "expires_at"):
        assert re.fullmatch(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", payload[k])
    body = "## Incident\n\nclass: external_feed\n"
    assert strip_brake_marker(body + marker + "\n") == body
    assert strip_brake_marker("head\n" + marker + "\ntail") == "head\ntail"
    assert strip_brake_marker(body) == body


# --- clearing ---------------------------------------------------------------------


def test_cleared_by_later_green_run_of_the_job():
    opened = T0 + 25 * MIN
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN)]
    kw = dict(quiet_s=90 * MIN, rollups=ROLLUPS)
    assert not is_cleared("security: audit", opened, cands, {}, now=T0 + 40 * MIN, **kw)
    green = _green(4, T0 + 30 * MIN)
    assert is_cleared("security: audit", opened, cands + [green], {}, now=T0 + 40 * MIN, **kw)
    # Not yet completed at `now`: not evidence.
    assert not is_cleared("security: audit", opened, cands + [green], {},
                          now=T0 + 33 * MIN, **kw)
    # A green push:main run created after the incident opened also clears.
    main = _main(T0 + 30 * MIN, [_job("static-checks / security: audit", "success",
                                      T0 + 35 * MIN)])
    assert is_cleared("security: audit", opened, cands, {WF: main}, now=T0 + 40 * MIN, **kw)


def test_not_cleared_by_quiet_with_zero_candidates():
    opened = T0 + 25 * MIN
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN)]
    assert not is_cleared("security: audit", opened, cands, {}, now=T0 + 10 * 3600,
                          quiet_s=90 * MIN, rollups=ROLLUPS)


def test_cleared_by_quiet_when_a_candidate_completed_the_job():
    opened = T0 + 25 * MIN
    cands = [_fail(1, T0), _fail(2, T0 + 10 * MIN), _fail(3, T0 + 20 * MIN)]
    # Created before the incident opened (so not a "later green"), completed in the quiet span.
    slow = _cand(4, T0 + 22 * MIN, [_job("static-checks / security: audit", "success",
                                         T0 + 4 * 3600)], conclusion="success")
    now = T0 + 5 * 3600
    assert is_cleared("security: audit", opened, cands + [slow], {}, now=now,
                      quiet_s=90 * MIN, rollups=ROLLUPS)
    # A failure inside the quiet span blocks the quiet clear.
    late_fail = _cand(5, T0 + 22 * MIN, [_job("static-checks / security: audit", "failure",
                                              T0 + 4 * 3600 + 30 * MIN)])
    assert not is_cleared("security: audit", opened, cands + [slow, late_fail], {}, now=now,
                          quiet_s=90 * MIN, rollups=ROLLUPS)


# --- thrash -------------------------------------------------------------------------


def test_thrash_rule():
    base = [1] * 14 * 24
    assert thrash(6, base, 0.9, 10, min_failed=6) is True
    assert thrash(6, base, 0.5, 10, min_failed=6) is False
    assert thrash(5, base, 0.9, 10, min_failed=6) is False
    assert thrash(6, base, 0.9, 7, min_failed=6) is False
    assert thrash(8, [3] * 24, 0.9, 10, min_failed=6) is False  # 3 x median 3 = 9
    assert thrash(6, [], 0.9, 10, min_failed=6) is True


def _state(**kw):
    base = {"job": "security: audit", "class": "external_feed", "rule": "C",
            "opened_ts": _ts("2026-10-31T01:02:00Z"),
            "first_failed_ts": _ts("2026-10-31T00:41:00Z"),
            "last_failed_ts": _ts("2026-10-31T01:01:36Z"), "prs": [20234, 20304, 20305],
            "runs": [111, 222], "signature": "Found N vulnerabilities",
            "excerpt": "Found 4 vulnerabilities", "brake_state": "none", "fingerprint": "abc123"}
    base.update(kw)
    return base


def test_issue_body_shows_utc_and_new_york_times_prs_runs_and_action():
    body = issue_body(_state(), repo="cairnea/KinDash")
    assert "2026-10-31 01:02 UTC (2026-10-30 21:02 EDT)" in body
    assert "#20234, #20304, #20305" in body
    assert "https://github.com/cairnea/KinDash/actions/runs/111" in body
    assert "external feed" in body and "never mute the check" in body
    poison = issue_body(_state(**{"class": "poisoner", "poisoner_pr": 20234}),
                        repo="cairnea/KinDash")
    assert "> " in poison and "dequeue PR #20234 under the queue lock" in poison


def test_fix_prompt_carries_class_instruction_and_safety_rules():
    p = fix_prompt(_state(), repo="cairnea/KinDash")
    assert "restore the gate" in p
    assert "already touches the inputs" in p and "instead of opening a parallel one" in p
    assert "Never mute" in p and "Never mutate the merge queue" in p
    assert "arm auto-merge" in p
    m = fix_prompt(_state(**{"class": "main_regression"}), repo="cairnea/KinDash")
    assert "revert the merge that broke `security: audit` on main" in m
