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


def gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    print(f"$ gh {' '.join(args)}\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def probe_mutations() -> None:  # noqa: C901 - spike probe, deliberate length
    repo = f"{OWNER}/gh-projects-spike-003"

    # --- introspect blockedBy input shapes before anything else -----------------
    print("=== introspect AddBlockedByInput / RemoveBlockedByInput ===")
    input_shapes = gh_graphql(
        "query { t1: __type(name: \"AddBlockedByInput\") { inputFields { name type { name kind ofType { name } } } }"
        " t2: __type(name: \"RemoveBlockedByInput\") { inputFields { name type { name kind ofType { name } } } } }"
    )
    print(json.dumps(input_shapes, indent=2))

    # AddBlockedByInput fields: issueId (NON_NULL ID), blockingIssueId (NON_NULL ID)
    # RemoveBlockedByInput fields: same
    # (verified from live introspection before this run)

    # --- create scratch repo + 3 issues -----------------------------------------
    print("=== create scratch repo ===")
    gh("repo", "create", repo, "--private",
       "--description", "003 Phase 0 spike scratch — safe to delete")
    urls = [gh("issue", "create", "-R", repo, "-t", f"spike issue {i}",
               "-b", "scratch").strip() for i in (1, 2, 3)]
    print("issue urls:", urls)

    # --- resolve node IDs for the 3 issues --------------------------------------
    ids = gh_graphql(
        'query { repository(owner: "%s", name: "gh-projects-spike-003") {'
        " i1: issue(number: 1) { id } i2: issue(number: 2) { id }"
        " i3: issue(number: 3) { id } } }" % OWNER)
    print(json.dumps(ids, indent=2))
    i1, i2, i3 = (ids["data"]["repository"][k]["id"] for k in ("i1", "i2", "i3"))

    # --- Project v2 mutations (require project scope — try App token first) ------
    tok = app_token()

    print("=== createProjectV2 (App token) ===")
    owner_id = gh_graphql(
        'query { user(login: "%s") { id } }' % OWNER)["data"]["user"]["id"]
    proj = gh_graphql(
        "mutation { createProjectV2(input: {ownerId: \"%s\", title: "
        "\"spike-003-scratch\"}) { projectV2 { id number } } }" % owner_id,
        token=tok)
    print(json.dumps(proj, indent=2))

    pid: str | None = None
    fid: str | None = None
    item_a: str | None = None
    item_b: str | None = None

    if proj.get("data", {}).get("createProjectV2"):
        pid = proj["data"]["createProjectV2"]["projectV2"]["id"]

        # --- field creation (Phase B pre-verification) --------------------------
        print("=== createProjectV2Field (App token) ===")
        field = gh_graphql(
            "mutation { createProjectV2Field(input: {projectId: \"%s\", "
            "dataType: NUMBER, name: \"Score\"}) { projectV2Field { "
            "... on ProjectV2FieldCommon { id name } } } }" % pid,
            token=tok)
        print(json.dumps(field, indent=2))
        if field.get("data", {}).get("createProjectV2Field"):
            fid = field["data"]["createProjectV2Field"]["projectV2Field"]["id"]

        # --- add items + aliased batched field updates --------------------------
        print("=== addProjectV2ItemById + aliased batch updateProjectV2ItemFieldValue (App token) ===")
        add = gh_graphql(
            "mutation { a: addProjectV2ItemById(input: {projectId: \"%s\", contentId: \"%s\"}) "
            "{ item { id } } b: addProjectV2ItemById(input: {projectId: \"%s\", contentId: \"%s\"}) "
            "{ item { id } } }" % (pid, i1, pid, i2),
            token=tok)
        print(json.dumps(add, indent=2))
        if add.get("data", {}).get("a") and add.get("data", {}).get("b"):
            item_a = add["data"]["a"]["item"]["id"]
            item_b = add["data"]["b"]["item"]["id"]

        if fid and item_a and item_b:
            batch = gh_graphql(
                "mutation { u1: updateProjectV2ItemFieldValue(input: {projectId: \"%s\", itemId: \"%s\","
                " fieldId: \"%s\", value: {number: 5}}) { projectV2Item { id } }"
                " u2: updateProjectV2ItemFieldValue(input: {projectId: \"%s\", itemId: \"%s\","
                " fieldId: \"%s\", value: {number: 8}}) { projectV2Item { id } } }"
                % (pid, item_a, fid, pid, item_b, fid),
                token=tok)
            print(json.dumps(batch, indent=2))
        else:
            print("SKIP batch field update — missing fid or item ids")
    else:
        print("createProjectV2 failed — skipping Project sub-probes")

    # --- sub-issue add + reorder (ambient credential, repo scope) ---------------
    print("=== addSubIssue (ambient credential) ===")
    r1 = gh_graphql(
        "mutation { addSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\"}) "
        "{ issue { number } } }" % (i1, i2))
    print(json.dumps(r1, indent=2))

    r2 = gh_graphql(
        "mutation { addSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\"}) "
        "{ issue { number } } }" % (i1, i3))
    print(json.dumps(r2, indent=2))

    print("=== reprioritizeSubIssue — move i3 after i2 (ambient credential) ===")
    # afterId/beforeId required per introspection; pass i2's node id as afterId
    r3 = gh_graphql(
        "mutation { reprioritizeSubIssue(input: {issueId: \"%s\", subIssueId: \"%s\", afterId: \"%s\"}) "
        "{ issue { subIssues(first: 5) { nodes { number } } } } }" % (i1, i3, i2))
    print(json.dumps(r3, indent=2))

    # --- native dependency add (ambient credential) -----------------------------
    # Input: issueId = the issue being blocked, blockingIssueId = the blocker
    # i.e. addBlockedBy(issueId=i2, blockingIssueId=i3) means "i2 is blocked by i3"
    print("=== addBlockedBy (ambient credential) ===")
    dep_add = gh_graphql(
        "mutation { addBlockedBy(input: {issueId: \"%s\", blockingIssueId: \"%s\"}) "
        "{ clientMutationId } }" % (i2, i3))
    print(json.dumps(dep_add, indent=2))

    # read back to confirm blockedBy relationship
    print("=== blockedBy read-back (ambient credential) ===")
    readback = gh_graphql(
        'query { repository(owner: "%s", name: "gh-projects-spike-003") { issue(number: 2) '
        "{ blockedBy(first: 5) { totalCount nodes { number } } } } }" % OWNER)
    print(json.dumps(readback, indent=2))

    # --- remove dependency (ambient credential) ---------------------------------
    print("=== removeBlockedBy (ambient credential) ===")
    dep_rm = gh_graphql(
        "mutation { removeBlockedBy(input: {issueId: \"%s\", blockingIssueId: \"%s\"}) "
        "{ clientMutationId } }" % (i2, i3))
    print(json.dumps(dep_rm, indent=2))

    # --- cleanup -----------------------------------------------------------------
    print("=== cleanup: deleteProjectV2 ===")
    if pid:
        del_proj = gh_graphql(
            "mutation { deleteProjectV2(input: {projectId: \"%s\"}) { clientMutationId } }" % pid,
            token=tok)
        print(json.dumps(del_proj, indent=2))
    else:
        print("No project to delete")

    print("=== cleanup: delete scratch repo ===")
    del_out = subprocess.run(
        ["gh", "repo", "delete", repo, "--yes"], capture_output=True, text=True)
    print(f"stdout: {del_out.stdout}\nstderr: {del_out.stderr}\nreturncode: {del_out.returncode}")

    if del_out.returncode != 0:
        # Fallback: try REST DELETE under App token
        print("=== cleanup fallback: REST DELETE under App token ===")
        import os
        env = dict(os.environ, GH_TOKEN=tok) if tok else None
        rest_del = subprocess.run(
            ["gh", "api", "-X", "DELETE", f"repos/{repo}"],
            capture_output=True, text=True, env=env)
        print(f"stdout: {rest_del.stdout}\nstderr: {rest_del.stderr}\nreturncode: {rest_del.returncode}")

        if rest_del.returncode != 0:
            # Final fallback: archive + rename
            print("=== cleanup final fallback: archive + rename ===")
            arc = subprocess.run(
                ["gh", "api", "-X", "PATCH", f"repos/{repo}",
                 "-f", "archived=true", "-f", "name=DELETE-ME-gh-projects-spike-003"],
                capture_output=True, text=True)
            print(f"archive/rename: stdout={arc.stdout}\nstderr={arc.stderr}\nreturncode={arc.returncode}")
            print("NOTE: manual deletion required — repo archived and renamed DELETE-ME-gh-projects-spike-003")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auth"
    {"auth": probe_auth, "introspect": probe_introspect,
     "mutations": probe_mutations}[cmd]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
