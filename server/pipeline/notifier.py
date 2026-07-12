"""Minimal frame-diffing notifier (spec §8 phase 2; full engine lands in Phase 4).

Compares consecutive DashboardState frames and emits plotroom-shaped
NotificationEvent dicts for conditions that NEWLY became active. The first
frame is a baseline: nothing fires on it (avoids a notification storm at
server start). rendered strings mirror plotroom server/notifier.ts
renderNotification — the browser displays them verbatim.
"""
from __future__ import annotations

_LABELS = {"ci-failed": "CI failed", "ready": "ready to merge", "overdue": "overdue"}


def _active_conditions(pr: dict) -> set[str]:
    s = pr.get("stage") or {}
    out: set[str] = set()
    if s.get("substate") == "ci-failed":
        out.add("ci-failed")
    if s.get("stage") == "ready":
        out.add("ready")
    if s.get("overdue"):
        out.add("overdue")
    return out


def _render(repo: str, number: int, title: str, kind: str) -> dict:
    return {"title": f"{repo}#{number} {_LABELS[kind]}", "body": title}


class TransitionNotifier:
    def __init__(self) -> None:
        self._prev: dict[tuple[str, int], set[str]] | None = None

    def diff(self, state: dict) -> list[dict]:
        current: dict[tuple[str, int], set[str]] = {}
        prs: dict[tuple[str, int], dict] = {}
        for repo in state.get("repos", []):
            for pr in repo.get("prs", []):
                key = (repo["repo"], pr["number"])
                current[key] = _active_conditions(pr)
                prs[key] = pr
        if self._prev is None:            # baseline: record, never fire
            self._prev = current
            return []
        events: list[dict] = []
        for key, active in current.items():
            new = active - self._prev.get(key, set())
            for kind in sorted(new):
                repo, number = key
                title = prs[key].get("title", "")
                events.append({
                    "repo": repo, "prNumber": number, "title": title,
                    "type": kind, "detail": "",
                    "rendered": _render(repo, number, title, kind),
                })
        self._prev = current
        return events
