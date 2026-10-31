"""Pure systemic-red evaluator over merge-queue observations (no I/O).

A job is *systemic* when it fails across the merge queue regardless of which PR is
in the candidate (spec 2026-10-31-cicd-doctor-systemic-red §3-§4, §6a). This module
holds the rule (``detect``), the clear test (``is_cleared``), the log signature and
fingerprint, the classifier, the KinDash brake-marker contract and the thrash rule.
The polling loop that feeds it lives elsewhere; everything here is deterministic over
its arguments, with ``now`` passed in so a replay can step the clock.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass

# A job counts as failed only once it has completed with one of these. startup_failure
# is included so a job that cannot start on every candidate surfaces (and classifies
# as infra) instead of hiding.
_FAILED = frozenset({"failure", "timed_out", "startup_failure"})
_GREEN = "success"
# Completed outcomes that say nothing about the job's health.
_NOT_EVIDENCE = frozenset({"", "cancelled", "skipped", "neutral", "stale"})

BRAKE_CLASSES = frozenset({"external_feed", "main_regression"})
BRAKE_BY = "viberpit-cicd-doctor"

_QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/(?P<base>.+)/pr-(?P<pr>\d+)-(?P<sha>[0-9a-fA-F]+)$")
_MARKER_RE = re.compile(r"<!-- viberpit:systemic-brake \{.*?\} -->\n?", re.DOTALL)
_ADVISORY_RE = re.compile(r"\b(?:GHSA(?:-[0-9a-z]{4}){3}|CVE-\d{4}-\d{4,})\b", re.IGNORECASE)
_ADVISORY_MARK = re.compile(r"\b(?:GHSA-|CVE-)", re.IGNORECASE)
# Runner-level failures (§4b). Matched against the NORMALIZED signature, so every
# entry must survive normalization (``exit code 137`` is protected below).
_INFRA_SIGNATURES = (
    "received a shutdown signal",
    "lost communication with the server",
    "no space left on device",
    "exit code 137",
    "the operation was canceled",
)
_PROTECTED_RE = re.compile(_ADVISORY_RE.pattern + r"|\bexit code 137\b", re.IGNORECASE)
_LOG_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"
)
_NODE_MODULES_RE = re.compile(r"node_modules/\S+")
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")
_ERROR_LIKE = re.compile(r"error|failed|✗|FAIL", re.IGNORECASE)
_EXCERPT_LINES = 40
_PLACEHOLDER = 0xE000  # Unicode private-use area


@dataclass(frozen=True)
class JobObs:
    """One job of a workflow run, as the Actions jobs API reports it."""
    name: str
    conclusion: str  # "" while not completed
    status: str  # queued | in_progress | completed
    completed_ts: float | None
    job_id: int


@dataclass(frozen=True)
class RunObs:
    """One workflow run (a merge_group candidate or a push:main run) with its jobs."""
    run_id: int
    workflow_id: int
    event: str
    head_branch: str
    head_sha: str
    created_ts: float
    status: str
    conclusion: str
    jobs: tuple[JobObs, ...]


@dataclass(frozen=True)
class Hit:
    """Job ``job`` met rule ``rule`` ("C" = distinct PRs, "M" = main red)."""
    job: str
    rule: str  # "C" | "M"
    prs: tuple[int, ...]
    first_failed_ts: float
    last_failed_ts: float
    failing_runs: tuple[int, ...]
    failing_job_ids: tuple[int, ...]


@dataclass(frozen=True)
class _Fail:
    pr: int
    ts: float
    run_id: int
    job_id: int


def normalize_job(name: str) -> str:
    """Drop the reusable-workflow prefix: the segment after the last ``" / "``."""
    return name.rsplit(" / ", 1)[-1]


def pr_of_branch(head_branch: str) -> tuple[int, str] | None:
    """``gh-readonly-queue/<base>/pr-<N>-<sha>`` → ``(N, sha)``; else None.

    The sha is the candidate's BASE (main or the entry ahead), not the PR head.
    """
    m = _QUEUE_BRANCH.match(head_branch)
    return (int(m.group("pr")), m.group("sha")) if m else None


def _done(job: JobObs, now: float) -> bool:
    """Completed as of ``now`` (a replay may hold jobs that complete later)."""
    if job.status != "completed":
        return False
    return job.completed_ts is None or job.completed_ts <= now


def _job_ts(job: JobObs, run: RunObs) -> float:
    return job.completed_ts if job.completed_ts is not None else run.created_ts


def _outcomes(run: RunObs, now: float, rollups: frozenset[str]) -> list[tuple[str, JobObs]]:
    """(normalized name, job) for the run's jobs completed by ``now``, rollups excluded."""
    out = []
    for j in run.jobs:
        name = normalize_job(j.name)
        if name not in rollups and _done(j, now):
            out.append((name, j))
    return out


def _hit(job: str, rule: str, fails: list[_Fail]) -> Hit:
    fails = sorted(fails, key=lambda f: (f.ts, f.run_id))
    return Hit(job=job, rule=rule, prs=tuple(sorted({f.pr for f in fails})),
               first_failed_ts=fails[0].ts, last_failed_ts=fails[-1].ts,
               failing_runs=tuple(dict.fromkeys(f.run_id for f in fails)),
               failing_job_ids=tuple(f.job_id for f in fails))


def detect(candidates: list[RunObs], mains: dict[int, RunObs], *, now: float,
           window_s: float, min_prs: int, rollups: frozenset[str]) -> list[Hit]:
    """Systemic jobs among ``candidates`` created in ``(now - window_s, now]`` (§3b).

    (C): the job failed on candidates of >= ``min_prs`` distinct PRs and no candidate
    created after the earliest of those failures ran it green. (M): the job failed on
    the latest push:main run of the candidate's workflow (``mains`` keyed by
    workflow_id) and on >= 1 candidate created after that run. One hit per job; (M)
    wins when both hold. Only jobs completed by ``now`` are evidence, so an
    in-progress run contributes the static checks that already failed.
    """
    rollups = frozenset(normalize_job(r) for r in rollups)
    fails: dict[str, list[_Fail]] = defaultdict(list)
    greens: dict[str, list[float]] = defaultdict(list)  # candidate created_ts
    m_fails: dict[str, list[_Fail]] = defaultdict(list)
    for run in candidates:
        if not (now - window_s < run.created_ts <= now):
            continue
        parsed = pr_of_branch(run.head_branch)
        if parsed is None:
            continue
        pr = parsed[0]
        main = mains.get(run.workflow_id)
        main_red = _main_failed_jobs(main, now, rollups) if main else frozenset()
        for name, j in _outcomes(run, now, rollups):
            if j.conclusion in _FAILED:
                f = _Fail(pr=pr, ts=_job_ts(j, run), run_id=run.run_id, job_id=j.job_id)
                fails[name].append(f)
                if main is not None and name in main_red and run.created_ts > main.created_ts:
                    m_fails[name].append(f)
            elif j.conclusion == _GREEN:
                greens[name].append(run.created_ts)
    hits: list[Hit] = []
    for name in sorted(set(fails) | set(m_fails)):
        if m_fails.get(name):
            hits.append(_hit(name, "M", m_fails[name]))
            continue
        fs = fails[name]
        if len({f.pr for f in fs}) < min_prs:
            continue
        earliest = min(f.ts for f in fs)
        if any(g > earliest for g in greens.get(name, ())):
            continue
        hits.append(_hit(name, "C", fs))
    return hits


def _main_failed_jobs(main: RunObs, now: float, rollups: frozenset[str]) -> frozenset[str]:
    return frozenset(n for n, j in _outcomes(main, now, rollups) if j.conclusion in _FAILED)


def is_cleared(job: str, opened_ts: float, candidates: list[RunObs], mains: dict[int, RunObs],
               *, now: float, quiet_s: float, rollups: frozenset[str]) -> bool:
    """§3c: cleared when a candidate or push:main run created after ``opened_ts`` ran
    ``job`` green, or when ``job`` has not failed for ``quiet_s`` while at least one
    candidate completed it in that span. Zero candidates is "not yet", never quiet."""
    job = normalize_job(job)
    rollups = frozenset(normalize_job(r) for r in rollups)
    if job in rollups:
        return True  # a rollup is never an incident; anything stale clears
    runs = [(r, True) for r in candidates] + [(r, False) for r in mains.values()]
    for run, _ in runs:
        for name, j in _outcomes(run, now, rollups):
            if name == job and j.conclusion == _GREEN and run.created_ts > opened_ts:
                return True
    quiet_from = now - quiet_s
    completed_in_span = False
    for run, is_candidate in runs:
        for name, j in _outcomes(run, now, rollups):
            if name != job or _job_ts(j, run) <= quiet_from:
                continue
            if j.conclusion in _FAILED:
                return False
            if is_candidate and j.conclusion not in _NOT_EVIDENCE:
                completed_in_span = True
    return completed_in_span


def _normalize_line(line: str) -> str:
    """Strip timestamps, shas, node_modules paths and numbers; keep advisory ids."""
    protected: list[str] = []

    def _keep(m: re.Match[str]) -> str:
        protected.append(m.group(0))
        return chr(_PLACEHOLDER + len(protected) - 1)  # a non-digit stand-in

    s = _PROTECTED_RE.sub(_keep, line.replace("##[error]", ""))
    s = _TIMESTAMP_RE.sub("", s)
    s = _NODE_MODULES_RE.sub("node_modules/…", s)
    s = _SHA_RE.sub("", s)
    s = _NUMBER_RE.sub("N", s)
    for i, text in enumerate(protected):
        s = s.replace(chr(_PLACEHOLDER + i), text)
    return re.sub(r"\s+", " ", s).strip()


def signature(log_text: str) -> tuple[str, str]:
    """(normalized signature line, raw excerpt of <= 40 lines) from a job log (§4a).

    The first ``##[error]`` line wins; else, within the last 40 lines, the first line
    matching ``error|failed|✗|FAIL``; else the last non-empty line.
    """
    lines = [_LOG_TS_PREFIX.sub("", ln) for ln in log_text.splitlines()]
    if not any(ln.strip() for ln in lines):
        return ("", "")
    idx = next((i for i, ln in enumerate(lines) if "##[error]" in ln), None)
    if idx is None:
        tail_start = max(0, len(lines) - _EXCERPT_LINES)
        idx = next((i for i in range(tail_start, len(lines)) if _ERROR_LIKE.search(lines[i])),
                   None)
    if idx is None:
        idx = max(i for i, ln in enumerate(lines) if ln.strip())
    start = max(0, idx - (_EXCERPT_LINES - 10))
    excerpt = lines[start:start + _EXCERPT_LINES]
    return (_normalize_line(lines[idx]), "\n".join(excerpt))


def fingerprint(repo: str, job: str, normalized_sig: str) -> str:
    """Stable incident id: sha1 over (repo, normalized job, signature), hex[:12]."""
    key = f"{repo}\n{normalize_job(job)}\n{normalized_sig}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _stack(run: RunObs, by_head: dict[str, RunObs], main_head: str | None) -> tuple[frozenset[int], bool]:
    """(PRs in the candidate's stack, complete?) by walking base sha → candidate head."""
    prs: set[int] = set()
    seen: set[int] = set()
    cur: RunObs | None = run
    while cur is not None and cur.run_id not in seen:
        seen.add(cur.run_id)
        parsed = pr_of_branch(cur.head_branch)
        if parsed is None:
            return frozenset(prs), False
        pr, base = parsed
        prs.add(pr)
        if main_head is not None and _sha_eq(base, main_head):
            return frozenset(prs), True
        cur = next((r for h, r in by_head.items() if _sha_eq(base, h)), None)
    return frozenset(prs), False


def _sha_eq(a: str, b: str) -> bool:
    """Equal shas, tolerating an abbreviated form on either side."""
    a, b = a.lower(), b.lower()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def classify(hit: Hit, candidates: list[RunObs], mains: dict[int, RunObs], *,
             normalized_sig: str, external_feed_re: str) -> str:
    """§4b, first match wins: external_feed → infra → poisoner → main_regression → unknown."""
    job = hit.job
    if re.search(external_feed_re, job) or _ADVISORY_MARK.search(normalized_sig):
        return "external_feed"

    failing_ids = set(hit.failing_job_ids)
    failing_runs = [c for c in candidates if c.run_id in set(hit.failing_runs)]
    if any(r.conclusion == "startup_failure" for r in failing_runs) or any(
        j.conclusion == "startup_failure" for r in failing_runs for j in r.jobs
        if j.job_id in failing_ids
    ):
        return "infra"
    low = normalized_sig.lower()
    if any(s in low for s in _INFRA_SIGNATURES):
        return "infra"

    # (M) means J is red on push:main, so poisoner's "J ran green on main" cannot
    # hold: checking it here keeps the §4b order.
    if hit.rule == "M":
        return "main_regression"
    if not failing_runs:
        return "unknown"
    by_head = {c.head_sha: c for c in candidates if pr_of_branch(c.head_branch)}
    wf = failing_runs[0].workflow_id
    main = mains.get(wf)
    main_head = main.head_sha if main else None
    stacks = [_stack(r, by_head, main_head) for r in failing_runs]
    common = frozenset.intersection(*(s for s, _ in stacks))
    all_complete = all(ok for _, ok in stacks)

    if common:
        main_green = main is not None and any(
            normalize_job(j.name) == job and j.status == "completed" and j.conclusion == _GREEN
            for j in main.jobs
        )
        if main_green and any(_only_stacks_with(p, job, candidates, by_head, main_head)
                              for p in common):
            return "poisoner"
        # A shared PR that isn't provably the poisoner: poisoner vs main_regression
        # is undecided, so never a brake class.
        return "unknown"
    return "main_regression" if all_complete else "unknown"


def _only_stacks_with(p: int, job: str, candidates: list[RunObs],
                      by_head: dict[str, RunObs], main_head: str | None) -> bool:
    """No candidate whose stack lacks PR ``p`` failed ``job``. A partial stack that
    lacks ``p`` in its known part may still hold it, so it also fails the proof."""
    for c in candidates:
        if pr_of_branch(c.head_branch) is None:
            continue
        if not any(normalize_job(j.name) == job and j.status == "completed"
                   and j.conclusion in _FAILED for j in c.jobs):
            continue
        prs, _complete = _stack(c, by_head, main_head)
        if p not in prs:
            return False
    return True


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def brake_marker(*, job: str, klass: str, incident: str, set_at: float, expires_at: float) -> str:
    """The KinDash brake-marker HTML comment (contract: cairnea/KinDash#20319)."""
    payload = {"job": job, "class": klass, "incident": incident, "set_at": _iso(set_at),
               "expires_at": _iso(expires_at), "by": BRAKE_BY}
    return f"<!-- viberpit:systemic-brake {json.dumps(payload, separators=(',', ':'))} -->"


def strip_brake_marker(body: str) -> str:
    """Remove every brake marker (and the newline right after it) from an issue body."""
    return _MARKER_RE.sub("", body)


def thrash(failed_last_hour: int, hourly_failed_14d: list[int], ratio: float,
           runs_in_window: int, *, min_failed: int) -> bool:
    """§6a queue thrash: failed/hour >= max(min_failed, 3 x 14-day hourly median) and
    a failure ratio >= 0.8 over >= 8 runs."""
    median = statistics.median(hourly_failed_14d) if hourly_failed_14d else 0
    return (failed_last_hour >= max(min_failed, 3 * median)
            and ratio >= 0.8 and runs_in_window >= 8)
