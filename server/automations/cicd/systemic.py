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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from viberpit.brand import SYSTEMIC_BRAKE_MARKER_PREFIX

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
_MARKER_RE = re.compile(re.escape(SYSTEMIC_BRAKE_MARKER_PREFIX) + r"\{.*?\} -->\n?", re.DOTALL)
_ADVISORY_RE = re.compile(r"\b(?:GHSA(?:-[0-9a-z]{4}){3}|CVE-\d{4}-\d{4,})\b", re.IGNORECASE)
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
    main_red_by_wf = {wf: _main_failed_jobs(m, now, rollups) for wf, m in mains.items()}
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
        main_red = main_red_by_wf.get(run.workflow_id, frozenset())
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
    if re.search(external_feed_re, job) or _ADVISORY_RE.search(normalized_sig):
        return "external_feed"

    failing_ids = set(hit.failing_job_ids)
    failing_run_ids = set(hit.failing_runs)
    failing_runs = [c for c in candidates if c.run_id in failing_run_ids]
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
    common, all_complete, suspect = _stack_verdict(job, failing_runs, candidates, mains)
    if suspect is not None:
        return "poisoner"
    # A shared PR that isn't provably the poisoner: poisoner vs main_regression is
    # undecided, so never a brake class.
    if common:
        return "unknown"
    return "main_regression" if all_complete else "unknown"


def poisoner_pr(hit: Hit, candidates: list[RunObs], mains: dict[int, RunObs]) -> int | None:
    """The PR that §4b proves poisons ``hit.job`` (shared by every failing stack, job
    green on push:main, and no failing candidate lacks it); None when unproven."""
    failing_run_ids = set(hit.failing_runs)
    failing_runs = [c for c in candidates if c.run_id in failing_run_ids]
    if not failing_runs:
        return None
    return _stack_verdict(hit.job, failing_runs, candidates, mains)[2]


def _stack_verdict(job: str, failing_runs: list[RunObs], candidates: list[RunObs],
                   mains: dict[int, RunObs]) -> tuple[frozenset[int], bool, int | None]:
    """(PRs common to every failing stack, all stacks complete?, proven poisoner)."""
    by_head = {c.head_sha: c for c in candidates if pr_of_branch(c.head_branch)}
    main = mains.get(failing_runs[0].workflow_id)
    main_head = main.head_sha if main else None
    stacks = [_stack(r, by_head, main_head) for r in failing_runs]
    common = frozenset.intersection(*(s for s, _ in stacks))
    all_complete = all(ok for _, ok in stacks)
    main_green = main is not None and any(
        normalize_job(j.name) == job and j.status == "completed" and j.conclusion == _GREEN
        for j in main.jobs
    )
    suspect = None
    if main_green:
        suspect = next((p for p in sorted(common)
                        if _only_stacks_with(p, job, candidates, by_head, main_head)), None)
    return common, all_complete, suspect


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
    return f"{SYSTEMIC_BRAKE_MARKER_PREFIX}{json.dumps(payload, separators=(',', ':'))} -->"


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


# --- incident issue body and fix-session prompt (spec §5b, §5d) -----------------

_NY = ZoneInfo("America/New_York")
_CLASS_LABEL = {
    "external_feed": "external feed",
    "main_regression": "main regression",
    "poisoner": "poisoner",
    "infra": "infra",
    "unknown": "unknown",
}
# §4b class-specific instruction for a fix session (brake classes only dispatch).
_FIX_INSTRUCTION = {
    "external_feed": ("The external feed moved; restore the gate (overrides or bumps, or "
                      "the repo's documented baseline procedure); never mute the check."),
    "main_regression": "Fix forward or revert the merge that broke `{job}` on main.",
}


def class_label(klass: str) -> str:
    return _CLASS_LABEL.get(klass, klass)


def when(ts: float | None) -> str:
    """``2026-10-31 01:02 UTC (2026-10-30 21:02 EDT)``; ``-`` for None."""
    if ts is None:
        return "-"
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{utc:%Y-%m-%d %H:%M} UTC ({utc.astimezone(_NY):%Y-%m-%d %H:%M %Z})"


def _run_url(repo: str, run_id: object) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def recommended_action(state: dict) -> str:
    klass, job = state.get("class", "unknown"), state.get("job", "")
    if klass == "poisoner":
        p = state.get("poisoner_pr")
        who = f"PR #{p}" if p else "the shared PR"
        return (f"{who} poisons every candidate queued behind it. PR Doctor owns its "
                f"failing CI. Ask the owner to dequeue {who} under the queue lock; the "
                "Doctor never touches the merge queue and never brakes a poisoner.")
    if klass == "infra":
        return ("Runner-level failure (shutdown, lost runner, disk, OOM). Follow the infra "
                "runbook; no brake and no fix session.")
    if klass in _FIX_INSTRUCTION:
        return _FIX_INSTRUCTION[klass].format(job=job)
    return "Alert only: triage the signature by hand; no brake and no fix session."


def dequeue_block(pr: object, *, repo: str) -> str:
    """The owner's lock-gated dequeue command for a poisoner (§5b); never run here."""
    n = pr if pr else "<N>"
    cmd = ("gh api graphql -f query='mutation($id:ID!){dequeuePullRequest(input:{id:$id})"
           "{clientMutationId}}' -f id=\"$(gh pr view " f"{n} -R {repo}" " --json id -q .id)\"")
    return ("The owner runs this under the KinDash merge-queue lock; the Doctor never runs it:"
            f"\n\n```sh\n{cmd}\n```")


def issue_title(state: dict) -> str:
    n = len(state.get("prs") or ())
    return (f"CI systemic red: `{state.get('job', '')}` failing on {n} PRs "
            f"({class_label(state.get('class', 'unknown'))})")


def issue_body(state: dict, *, repo: str) -> str:
    """Incident issue body (§5b). The brake marker is appended separately."""
    job, klass = state.get("job", ""), state.get("class", "unknown")
    prs = ", ".join(f"#{p}" for p in state.get("prs") or ()) or "-"
    runs = "\n".join(f"- {_run_url(repo, r)}" for r in (state.get("runs") or ())[:10]) or "- none"
    action = recommended_action(state)
    if klass == "poisoner":
        action += "\n\n" + dequeue_block(state.get("poisoner_pr"), repo=repo)
    brake = state.get("brake_state", "none")
    if brake == "set" and state.get("brake_expires_ts") is not None:
        brake = f"set, expires {when(state.get('brake_expires_ts'))}"
    rate = state.get("failed_per_hour")
    excerpt = (state.get("excerpt") or "").replace("```", "`​``")
    return "\n".join([
        f"ViberPit CI/CD Doctor detected a systemic red in `{repo}`.",
        "",
        f"- **Job:** `{job}`",
        f"- **Class:** {class_label(klass)}",
        f"- **Rule:** {state.get('rule', '-')}",
        f"- **PRs failing it:** {prs}",
        f"- **First failure:** {when(state.get('first_failed_ts'))}",
        f"- **Last failure:** {when(state.get('last_failed_ts'))}",
        f"- **Opened:** {when(state.get('opened_ts'))}",
        f"- **Brake:** {brake}",
        *([f"- **Failed merge_group runs/hour:** {rate}"] if rate is not None else []),
        f"- **Signature:** `{state.get('signature') or '-'}`",
        "",
        "**Failing runs**",
        runs,
        "",
        "**Recommended action**",
        "",
        action,
        "",
        "<details><summary>Log excerpt</summary>",
        "",
        "```",
        excerpt,
        "```",
        "</details>",
        "",
        f"Incident `{state.get('fingerprint', '')}`. Closing this issue lifts the brake.",
    ])


def fix_prompt(state: dict, *, repo: str) -> str:
    """Seed for the one fix session per incident (§5d)."""
    job, klass = state.get("job", ""), state.get("class", "unknown")
    runs = "\n".join(f"- {_run_url(repo, r)}" for r in (state.get("runs") or ())[:5]) or "- none"
    instruction = _FIX_INSTRUCTION.get(klass, recommended_action(state)).format(job=job)
    return "\n".join([
        f"The merge queue in {repo} is systemically red: `{job}` fails on every candidate "
        f"(PRs {', '.join(f'#{p}' for p in state.get('prs') or ()) or '-'}), "
        f"class {class_label(klass)}.",
        "",
        f"Signature: `{state.get('signature') or '-'}`",
        "",
        "Example failing runs:",
        runs,
        "",
        "Log excerpt:",
        "```",
        (state.get("excerpt") or "")[:4000],
        "```",
        "",
        f"What to do: {instruction}",
        "",
        "Rules:",
        f"1. First search the open PRs in {repo} for one that already touches the inputs of "
        f"`{job}` (its workflow step, the files it checks, or its dependencies). If one "
        "exists, finish or review that PR instead of opening a parallel one.",
        "2. Never mute, skip, or weaken the check, and never mark it non-required.",
        "3. Never mutate the merge queue (no enqueue, dequeue, or jump).",
        "4. Open a PR with the fix and arm auto-merge.",
    ])
