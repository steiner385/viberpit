#!/usr/bin/env python3
"""Phase 0 spike for spec 003: verify Projects v2 / sub-issue / dependency APIs.

Throwaway verification harness (constitution III). Never imported by server/.
Usage:
    python scripts/spike_gh_projects_v2.py auth
    python scripts/spike_gh_projects_v2.py introspect
    python scripts/spike_gh_projects_v2.py mutations   # Task 2
"""
from __future__ import annotations

import json
import subprocess
import sys

OWNER = "steiner385"


def gh_graphql(query: str, token: str | None = None, **fields: str) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        cmd += ["-f", f"{key}={value}"]
    env = None
    if token:
        import os
        env = dict(os.environ, GH_TOKEN=token)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    out = json.loads(proc.stdout or "{}")
    if proc.returncode != 0 or out.get("errors"):
        out["_spike_stderr"] = proc.stderr.strip()
    return out


def app_token() -> str | None:
    sys.path.insert(0, "server")
    try:
        from github.app_auth import token_for_owner
        return token_for_owner(OWNER)
    except Exception as exc:  # noqa: BLE001 - spike reports, never hides
        print(f"app token unavailable: {exc!r}")
        return None


PROJECTS_READ = (
    'query { user(login: "%s") { projectsV2(first: 20) '
    "{ totalCount nodes { id number title closed updatedAt } } } }" % OWNER
)


def probe_auth() -> None:
    print("=== ambient gh credential (user) ===")
    print(json.dumps(gh_graphql(PROJECTS_READ), indent=2))
    print("=== scopes of ambient credential ===")
    subprocess.run(["gh", "auth", "status"])
    tok = app_token()
    print("=== GitHub App installation token ===")
    if tok is None:
        print("NO APP TOKEN — record as finding")
    else:
        print(json.dumps(gh_graphql(PROJECTS_READ, token=tok), indent=2))


INTROSPECT_MUTATIONS = """
query { __schema { mutationType { fields { name } } } }
"""

INTROSPECT_ITEMS_ARGS = """
query { __type(name: "ProjectV2") { fields { name args { name type { name kind } } } } }
"""

INTROSPECT_ISSUE_FIELDS = """
query { __type(name: "Issue") { fields { name } } }
"""


def probe_introspect() -> None:
    muts = gh_graphql(INTROSPECT_MUTATIONS)
    names = [f["name"] for f in muts["data"]["__schema"]["mutationType"]["fields"]]
    interesting = [n for n in names if any(
        k in n.lower() for k in ("depend", "block", "subissue", "sub_issue", "projectv2"))]
    print("=== mutation names (dependency/sub-issue/projectV2) ===")
    print(json.dumps(sorted(interesting), indent=2))
    print("=== ProjectV2 field args (delta-read check: does items() take a filter?) ===")
    print(json.dumps(gh_graphql(INTROSPECT_ITEMS_ARGS), indent=2))
    print("=== Issue fields (blockedBy / blocking / parent / subIssues present?) ===")
    issue = gh_graphql(INTROSPECT_ISSUE_FIELDS)
    fields = [f["name"] for f in issue["data"]["__type"]["fields"]]
    print(json.dumps(sorted(f for f in fields if any(
        k in f.lower() for k in ("block", "parent", "sub"))), indent=2))


def probe_mutations() -> None:
    print("mutations probe is added in Task 2 of the spike")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    {"auth": probe_auth, "introspect": probe_introspect,
     "mutations": probe_mutations}[cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
