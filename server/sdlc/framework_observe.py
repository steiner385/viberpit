"""Pinned read-only framework inventory for opt-in SDLC observation.

Reads declarations only; does not execute repository code or validate admission.
All failures become explicit unavailable observations, never successful checks.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import PurePosixPath
import re
from typing import Callable

from server.sdlc.gates import FrameworkPolicySnapshot


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

        extra = " pullRequest(number:$number){headRefOid author{login}}" if pull_request else ""
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
                           author=(pr.get("author") or {}).get("login"))

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
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or PurePosixPath(path).is_absolute() or ".." in path.split("/")):
            raise ValueError("unsafe lock path")
        lock_oid, lock = blob(path)
        if lock is None or type(lock.get("schema_version")) is not int or lock["schema_version"] != 1:
            raise ValueError("missing or unsupported lock")
        entries = lock["frameworks"]
        if not isinstance(entries, list):
            raise ValueError("invalid inventory")
        ids = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not entry["id"] or entry["id"] in ids
                    or not isinstance(entry.get("state"), str)
                    or not isinstance(entry.get("concerns"), list)
                    or any(not isinstance(c, str) for c in entry["concerns"])):
                raise ValueError("invalid inventory entry")
            ids.add(entry["id"])
        return replace(snapshot, status="loaded", lock_oid=lock_oid,
                       frameworks=tuple(entries))
    except Exception:
        # Never expose gh stderr, credentials or repository content on this read
        # surface. Cancellation (BaseException) continues to propagate.
        return snapshot
