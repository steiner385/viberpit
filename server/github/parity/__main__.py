"""CLI: python -m server.github.parity --plotroom-url URL --repos a/b,c/d [--phase 0] [--json out.json]

Exit 0 = parity holds; 1 = parity mismatch; 2 = harness error (Plotroom unreachable etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from server.github.pr_poller import _default_query
from server.github.pr_status import parse_pr_response
from server.github.parity.harness import ParityReport, diff_snapshots
from server.github.parity.normalize import normalize_state


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


def compute_candidate(pairs, query_fn=_default_query):
    """One synchronous sweep; re-key (repo, branch) -> (repo, number)."""
    by_branch = parse_pr_response(query_fn(pairs), pairs)
    return {(repo, info.number): info for (repo, _branch), info in by_branch.items()}


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

    # Candidate: query each repo for the branches the oracle knows about.
    pairs = [(orc.repo, orc.branch) for orc in oracle.values() if orc.branch]
    pairs = [p for p in pairs if p[0] in repos] if repos else pairs
    candidate = compute_candidate(pairs)

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
