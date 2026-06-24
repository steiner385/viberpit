"""CLI: python -m server.github.parity --plotroom-url URL --repos a/b,c/d [--phase 0] [--json out.json]

Exit 0 = parity holds; 1 = parity mismatch; 2 = harness error (Plotroom unreachable etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from ccswitch import gitrepos
from server.github.pr_status import PullRequestInfo, _classify_checks, _rollup_state
from server.github.parity.harness import ParityReport, diff_snapshots
from server.github.parity.normalize import normalize_state
from server.pipeline.stage import derive_open_stage, derive_merged_stage


class HarnessError(RuntimeError):
    pass


def fetch_oracle(base_url: str, *, opener=urllib.request.urlopen) -> dict:
    url = base_url.rstrip("/") + "/api/state"
    try:
        with opener(url, timeout=15) as resp:
            code = getattr(resp, "getcode", lambda: 200)()
            if code != 200:
                raise HarnessError(f"Plotroom {url} returned HTTP {code}")
            return json.loads(resp.read() or b"{}")
    except HarnessError:
        raise
    except Exception as exc:  # transport / JSON / timeout
        raise HarnessError(f"could not fetch oracle {url}: {exc}") from exc


def _pr_list_query(repo: str) -> str:
    """GraphQL doc listing OPEN PRs for *repo* with the same statusCheckRollup ccswitch
    keys on, so the candidate's ``checks`` is classified identically to PrStatusPoller.
    Also requests isDraft and mergeStateStatus for the stage classifier (Phase 1a).
    """
    owner, _, name = repo.partition("/")
    return (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
        f"  pullRequests(states: OPEN, first: 100, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{"
        f"    nodes {{ number url isDraft mergeStateStatus"
        f"      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}"
        f"    }} }} }} }}"
    )


def _merged_pr_list_args(repo: str) -> list[str]:
    """gh CLI args to list recently-merged PRs for *repo*.

    Uses ``gh pr list`` (REST) rather than GraphQL because the merged-PR population
    is large and REST paginates cleanly via --limit.  We ask for 300 which is enough
    to cover the 7-day retention window for active repos.
    """
    return [
        "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--json", "number,url,mergedAt",
        "--limit", "300",
    ]


def compute_candidate_by_number(
    repos: list[str], *, run_gh: Callable = gitrepos._run_gh
) -> dict[tuple[str, int], PullRequestInfo]:
    """Candidate = every OPEN or recently-MERGED PR for each repo, keyed ``(repo, number)``.

    Open PRs use GraphQL with statusCheckRollup so checks classification matches
    PrStatusPoller exactly.  Merged PRs are fetched via gh pr list (REST); checks
    is irrelevant for merged PRs because the oracle drops that field (frozen rollup).
    """
    now = datetime.now(timezone.utc)
    out: dict[tuple[str, int], PullRequestInfo] = {}
    for repo in repos:
        # --- Open PRs (Phase 1a) ---
        proc = run_gh(["api", "graphql", "-f", f"query={_pr_list_query(repo)}"])
        if proc.returncode == 0:
            data = (json.loads(proc.stdout or "{}").get("data") or {}).get("repository") or {}
            for node in ((data.get("pullRequests") or {}).get("nodes") or []):
                checks = _classify_checks(_rollup_state(node))
                is_draft = bool(node.get("isDraft"))
                merge_state_status = node.get("mergeStateStatus")
                stage = derive_open_stage(checks, merge_state_status, is_draft)
                out[(repo, node["number"])] = PullRequestInfo(
                    number=node["number"], url=node.get("url", ""), state="open",
                    checks=checks, title="", is_draft=is_draft, stage=stage,
                )

        # --- Merged PRs (Phase 1, merged slice) ---
        mproc = run_gh(_merged_pr_list_args(repo))
        if mproc.returncode == 0:
            for item in json.loads(mproc.stdout or "[]"):
                merged_stage = derive_merged_stage(item.get("mergedAt"), now)
                if merged_stage is None:
                    continue  # off the board (older than retention) — skip
                out[(repo, item["number"])] = PullRequestInfo(
                    number=item["number"], url=item.get("url", ""), state="merged",
                    checks="none", title="", is_draft=False, stage=merged_stage,
                )
    return out


def render_report(report: ParityReport) -> str:
    lines = [
        f"matched: {report.matched}",
        f"mismatches: {len(report.mismatches)}",
        f"oracle-only: {len(report.oracle_only)}  candidate-only: {len(report.candidate_only)}",
    ]
    for m in report.mismatches:
        lines.append(f"  ✗ {m.key} .{m.field}: candidate={m.candidate!r} oracle={m.oracle!r}")
    for k in report.oracle_only:
        lines.append(f"  ● oracle-only {k}")
    for k in report.candidate_only:
        lines.append(f"  ○ candidate-only {k}")
    lines.append("PARITY OK" if report.ok else "PARITY FAILED")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.github.parity")
    ap.add_argument("--plotroom-url", default="http://127.0.0.1:4400")
    ap.add_argument("--repos", required=True, help="comma-separated owner/name list")
    ap.add_argument("--phase", type=int, default=0)
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    try:
        oracle = normalize_state(fetch_oracle(args.plotroom_url))
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 2

    # Restrict the oracle to the requested repos, then build the candidate over the
    # full open-PR population of those repos (number-keyed). diff_snapshots compares
    # only the shared PRs; population differences are reported, not failed.
    oracle = {k: v for k, v in oracle.items() if not repos or v.repo in repos}
    candidate = compute_candidate_by_number(repos)

    report = diff_snapshots(candidate, oracle, phase=args.phase)
    print(render_report(report))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(
                {
                    "matched": report.matched,
                    "mismatches": [vars(m) for m in report.mismatches],
                    "oracle_only": report.oracle_only,
                    "candidate_only": report.candidate_only,
                    "ok": report.ok,
                },
                fh,
                indent=2,
                default=list,
            )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
