"""Pinned read-only framework inventory for opt-in SDLC observation.

Reads declarations only; does not execute repository code or validate admission.
All failures become explicit unavailable observations, never successful checks.
"""
from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Callable

from server.sdlc.gates import FrameworkPolicySnapshot


from server.sdlc.framework_policy import policy

MAX_PAGES = 20


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def load_policy_snapshot(repo: str, *, pull_request: int | None = None,
                         run: Callable | None = None, token: str | None = None) -> FrameworkPolicySnapshot:
    snapshot = FrameworkPolicySnapshot(repo=repo, status="unavailable")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return snapshot
    owner, name = repo.split("/")
    try:
        if run is None:
            from viberpit import gitrepos
            run = gitrepos._run_gh
            token = gitrepos.app_token_for_repo(repo)

        def query(body: str, **variables) -> dict:
            declarations = ["$owner:String!", "$name:String!"]
            args = ["api", "graphql"]
            for key, value in {"owner": owner, "name": name, **variables}.items():
                args.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
                if key not in {"owner", "name"}:
                    declarations.append(f"${key}:{'Int' if isinstance(value, int) else 'String'}!")
            gql = f"query({','.join(declarations)}){{repository(owner:$owner,name:$name){{{body}}}}}"
            result = run([*args, "-f", f"query={gql}"], token, timeout=15)
            if result.returncode:
                raise ValueError("read failed")
            payload = json.loads(result.stdout)
            if payload.get("errors"):
                raise ValueError("partial read")
            data = payload["data"]["repository"]
            if not isinstance(data, dict):
                raise ValueError("repository unavailable")
            return data

        extra = " pullRequest(number:$number){headRefOid baseRefOid author{login}}" if pull_request else ""
        data = query("defaultBranchRef{target{oid}}" + extra,
                     **({"number": pull_request} if pull_request else {}))
        revision = data["defaultBranchRef"]["target"]["oid"]
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("unresolved default revision")
        pr = data.get("pullRequest") or {}
        head = pr.get("headRefOid")
        if head is not None and (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)):
            raise ValueError("unresolved subject revision")
        snapshot = replace(snapshot, policy_revision=revision, head_sha=head,
                           author=(pr.get("author") or {}).get("login"), base_sha=pr.get("baseRefOid"))

        def blob(path: str) -> tuple[str | None, dict | None]:
            value = query("object(expression:$expression){... on Blob{oid text isTruncated byteSize}}",
                          expression=f"{revision}:{path}").get("object")
            if value is None:
                return None, None
            if (value.get("isTruncated") or value.get("byteSize", 0) > 1_000_000
                    or not isinstance(value.get("text"), str)):
                raise ValueError("incomplete blob")
            obj = json.loads(value["text"])
            if not isinstance(obj, dict) or not isinstance(value.get("oid"), str):
                raise ValueError("invalid declaration")
            return value["oid"], obj

        contract_oid, contract = blob(".agents/contract.json")
        snapshot = replace(snapshot, contract_oid=contract_oid)
        if contract is None:
            return replace(snapshot, status="no_contract")
        if "frameworks" not in contract:
            return replace(snapshot, status="legacy_contract")
        path = contract["frameworks"]["lock"]
        if not policy.safe_path(path):
            raise ValueError("unsafe lock path")
        lock_oid, lock = blob(path)
        if lock is None:
            raise ValueError("missing framework lock")
        policy.validate_declarations(lock)
        snapshot = replace(snapshot, status="loaded", lock_oid=lock_oid,
                           frameworks=tuple(lock["frameworks"]),
                           framework_digest=policy.framework_digest(lock),
                           policy_digest=policy.canonical_digest(contract))
        if not pull_request or not _sha(snapshot.base_sha) or not _sha(snapshot.head_sha):
            return snapshot

        def pages(field: str, fields: str) -> tuple[list[dict], bool]:
            nodes: list[dict] = []
            cursor = None
            seen: set[str] = set()
            for _ in range(MAX_PAGES):
                after = ",after:$cursor" if cursor else ""
                variables = {"number": pull_request}
                if cursor:
                    variables["cursor"] = cursor
                result = query(f"pullRequest(number:$number){{headRefOid baseRefOid {field}(first:100{after}){{nodes{{{fields}}} pageInfo{{hasNextPage endCursor}}}}}}", **variables)
                subject = result["pullRequest"]
                if subject["headRefOid"] != head or subject["baseRefOid"] != snapshot.base_sha:
                    raise ValueError("subject moved")
                connection = subject[field]
                batch = connection["nodes"]
                if not isinstance(batch, list) or len(batch) > 100 or any(not isinstance(n, dict) for n in batch):
                    raise ValueError("invalid page")
                nodes.extend(batch)
                page = connection["pageInfo"]
                if type(page["hasNextPage"]) is not bool:
                    raise ValueError("invalid pagination flag")
                if page["hasNextPage"] is False:
                    return nodes, True
                cursor = page["endCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError("invalid cursor")
                seen.add(cursor)
            return nodes, False

        try:
            files, complete = pages("files", "path changeType")
            paths = [item["path"] for item in files]
            if any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in paths):
                raise ValueError("invalid path")
            # GraphQL omits previous filenames. Never infer a safe exclusion from
            # a rename or an unknown change type with only its destination path.
            complete = complete and all(item.get("changeType") in {"ADDED", "MODIFIED", "DELETED"} for item in files)
            reviews, reviews_complete = pages("reviews", "id author{login} editor{login} lastEditedAt isMinimized state submittedAt commit{oid} body")
            final = query("pullRequest(number:$number){headRefOid baseRefOid}", number=pull_request)["pullRequest"]
            if final["headRefOid"] != head or final["baseRefOid"] != snapshot.base_sha:
                raise ValueError("subject moved")
            snapshot = replace(snapshot, changed_paths=tuple(sorted(set(paths))), scope_complete=complete,
                               reviews=tuple(reviews), reviews_complete=reviews_complete)
        except Exception:
            # Partial/failed/moving reads cannot establish either a skip or a
            # receipt. Do not retain body text from a partially trusted fetch.
            pass
        return snapshot
    except Exception:
        # Never expose gh stderr, credentials or repository content on this read
        # surface. Cancellation (BaseException) continues to propagate.
        return snapshot
