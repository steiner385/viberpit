"""Decision-proposal routes (spec 2026-09-18 §6, §8).

The four taps on ``server/pm/decisions.py``: propose (which is the ONE way a
``decision`` hold ever gets set), the inbox list with its two-column guidance,
accept (the ONE way that hold ever clears, plus the GitHub comment) and reject.

The client/auth fixtures are imported from ``test_pm_api`` rather than copied:
one ``_client`` for the PM routes, so a change to how the app is built under test
cannot leave this module testing a different app (constitution VI).
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.tests.pm_ownership_fixtures import identity_repositories
from server.tests.test_pm_api import (
    IDENTITY_HEADERS,
    LAN_OPEN,
    _client,
    _register,
    _seed_one_pending,
)


@pytest.fixture(autouse=True)
def canonical_mutation_repositories(monkeypatch):
    # Autouse fixtures do not travel with an imported helper, so the ownership
    # seam test_pm_api installs for its own mutations is installed here too.
    monkeypatch.setattr(
        "server.automations.target_identity.resolve_repositories", identity_repositories)


OPTIONS = [
    {"key": "sqlite", "label": "Keep SQLite", "detail": "one file, no ops"},
    {"key": "pg", "label": "Move to Postgres", "detail": "concurrent writers"},
]


def _propose(c, wid, *, options=None, recommended="pg", rationale="two writers are coming"):
    return c.post(f"/api/pm/work-items/{wid}/decisions", json={
        "options": OPTIONS if options is None else options,
        "recommended_key": recommended,
        "rationale": rationale,
    }, headers=IDENTITY_HEADERS)


def _hold_of(c, wid):
    from server.db import db_session
    from server.db.models import WorkItem

    async def _read():
        async with db_session() as db:
            wi = await db.get(WorkItem, wid)
            return (wi.hold_kind, wi.hold_reason, wi.hold_by)
    return c.portal.call(_read)


def _stub_announce(monkeypatch):
    """Replace the GitHub comment with a recorder. The route reaches it through
    the module attribute, so patching here is the seam."""
    calls: list[tuple] = []

    def _fake(ref, *, label, detail, chosen_by):
        calls.append((ref, label, detail, chosen_by))

    monkeypatch.setattr("server.automations.backlog_github.announce_decision", _fake)
    return calls


def test_propose_list_accept_is_the_whole_decision_loop(monkeypatch):
    """Propose parks the item, the inbox lists it with guidance, accept clears the
    hold, records the key and says so on the issue."""
    calls = _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        r = _propose(c, wid)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["recommendedKey"] == "pg"
        assert [o["key"] for o in body["options"]] == ["sqlite", "pg"]
        assert body["proposedBy"] == IDENTITY_HEADERS["Tailscale-User-Login"]
        assert body["ref"] == "o/r#1" and body["projectSlug"] == "viberpit"
        assert body["chosenKey"] is None and body["guidance"] is None
        assert _hold_of(c, wid)[0] == "decision"

        listed = c.get("/api/pm/decisions", headers=IDENTITY_HEADERS)
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert [row["id"] for row in rows] == [body["id"]]
        row = rows[0]
        assert row["title"] == "Fix" and row["rationale"] == "two writers are coming"
        assert set(row["guidance"]) == {"accept", "reject"}
        assert row["guidance"]["accept"]["immediate"][0]["text"].startswith("The hold clears")

        acc = c.post(f"/api/pm/decisions/{body['id']}/accept",
                     json={"chosen_key": "sqlite"}, headers=IDENTITY_HEADERS)
        assert acc.status_code == 200, acc.text
        assert acc.json()["status"] == "accepted" and acc.json()["chosenKey"] == "sqlite"
        assert acc.json()["resolvedBy"] == IDENTITY_HEADERS["Tailscale-User-Login"]
        assert _hold_of(c, wid)[0] is None
        # exactly one comment, naming the option a person picked and who picked it
        assert calls == [("o/r#1", "Keep SQLite", "one file, no ops",
                          IDENTITY_HEADERS["Tailscale-User-Login"])]
        # and the inbox is empty again
        assert c.get("/api/pm/decisions", headers=IDENTITY_HEADERS).json() == []


def test_a_failed_github_comment_does_not_fail_the_accept(monkeypatch):
    """The accepted ROW is the decision; the comment is only its GitHub-side
    record, and it is posted after the commit."""
    def _boom(ref, *, label, detail, chosen_by):
        raise RuntimeError("gh is down")

    monkeypatch.setattr("server.automations.backlog_github.announce_decision", _boom)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        acc = c.post(f"/api/pm/decisions/{pid}/accept",
                     json={"chosen_key": "pg"}, headers=IDENTITY_HEADERS)
        assert acc.status_code == 200, acc.text
        assert _hold_of(c, wid)[0] is None


def test_a_stalled_issue_comment_does_not_hold_the_accept_open(monkeypatch):
    """F4: `gh issue comment` is the first use of that seam inside a request
    handler and `gitrepos._run_gh` bounds nothing, so the wait is bounded here.
    The decision is already committed by then — a slow comment must not become a
    hanging request."""
    import threading

    released = threading.Event()

    def _hang(ref, *, label, detail, chosen_by):
        released.wait(30)   # freed by the test once the response is in hand

    monkeypatch.setattr("server.automations.backlog_github.announce_decision", _hang)
    monkeypatch.setattr("server.api.pm.DECISION_COMMENT_TIMEOUT_S", 0.05)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        try:
            acc = c.post(f"/api/pm/decisions/{pid}/accept",
                         json={"chosen_key": "pg"}, headers=IDENTITY_HEADERS)
            assert acc.status_code == 200, acc.text
            assert acc.json()["chosenKey"] == "pg"
            assert _hold_of(c, wid)[0] is None   # the decision itself landed
        finally:
            released.set()


def test_second_proposal_on_the_same_item_is_a_conflict(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        assert _propose(c, wid).status_code == 201
        again = _propose(c, wid)
        assert again.status_code == 409
        assert again.json()["detail"]["error"] == "decision_conflict"
        assert "already pending" in again.json()["detail"]["message"]


def test_propose_validation_is_422_and_a_missing_item_is_404():
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        one = _propose(c, wid, options=[OPTIONS[0]])
        assert one.status_code == 422 and one.json()["detail"]["error"] == "bad_decision"
        assert "between 2 and 6 options" in one.json()["detail"]["message"]
        bad_key = _propose(c, wid, recommended="mysql")
        assert bad_key.status_code == 422 and "not one of" in bad_key.json()["detail"]["message"]
        no_why = _propose(c, wid, rationale="   ")
        assert no_why.status_code == 422 and "rationale" in no_why.json()["detail"]["message"]
        # nothing was written by any of the refusals
        assert _hold_of(c, wid)[0] is None
        assert c.get("/api/pm/decisions", headers=IDENTITY_HEADERS).json() == []
        missing = _propose(c, "no-such-item")
        assert missing.status_code == 404


def test_a_wrongly_typed_options_body_is_a_422_not_a_500():
    """The caller most likely to get the SHAPE wrong is a model driving
    ``propose_decision``; the types are pinned at the boundary so a bad one never
    reaches ``normalize_options`` (whose ``for raw in options`` would raise
    ``TypeError`` out of the route as a 500)."""
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        for bad in (5, True, "sqlite", {"key": "sqlite"}, ["sqlite", "pg"],
                    [{"key": "sqlite"}]):
            r = c.post(f"/api/pm/work-items/{wid}/decisions", json={
                "options": bad, "recommended_key": "sqlite", "rationale": "why"},
                headers=IDENTITY_HEADERS)
            assert r.status_code == 422, f"{bad!r} answered {r.status_code}: {r.text}"
        assert _hold_of(c, wid)[0] is None


def test_a_racing_second_proposal_is_the_same_409_as_the_friendly_path(monkeypatch):
    """``ux_decision_proposals_pending`` is what actually enforces one pending
    proposal per item; a proposer that passes the SELECT and loses at the flush is
    a conflict, not a 500."""
    from sqlalchemy.exc import IntegrityError

    async def _racing(db, wi, **kwargs):
        raise IntegrityError("INSERT INTO decision_proposals", {},
                             Exception("UNIQUE constraint failed"))

    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        monkeypatch.setattr("server.pm.decisions.propose", _racing)
        r = _propose(c, wid)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {"error": "decision_conflict",
                                      "message": "a decision is already pending"}


def test_accept_rejects_an_unknown_key_and_a_resolved_proposal(monkeypatch):
    calls = _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        bad = c.post(f"/api/pm/decisions/{pid}/accept",
                     json={"chosen_key": "mysql"}, headers=IDENTITY_HEADERS)
        assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_decision"
        assert calls == [] and _hold_of(c, wid)[0] == "decision"
        assert c.post(f"/api/pm/decisions/{pid}/accept",
                      json={"chosen_key": "pg"}, headers=IDENTITY_HEADERS).status_code == 200
        twice = c.post(f"/api/pm/decisions/{pid}/accept",
                       json={"chosen_key": "sqlite"}, headers=IDENTITY_HEADERS)
        assert twice.status_code == 409 and "already accepted" in twice.json()["detail"]["message"]
        assert len(calls) == 1
        assert c.post("/api/pm/decisions/nope/accept", json={"chosen_key": "pg"},
                      headers=IDENTITY_HEADERS).status_code == 404


def test_reject_hands_the_item_back_to_the_person(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        r = c.post(f"/api/pm/decisions/{pid}/reject", headers=IDENTITY_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected" and r.json()["chosenKey"] is None
        kind, reason, by = _hold_of(c, wid)
        assert (kind, reason) == ("owner_action", "decision proposal rejected")
        assert by == IDENTITY_HEADERS["Tailscale-User-Login"]
        assert c.get("/api/pm/decisions", headers=IDENTITY_HEADERS).json() == []
        twice = c.post(f"/api/pm/decisions/{pid}/reject", headers=IDENTITY_HEADERS)
        assert twice.status_code == 409
        assert c.post("/api/pm/decisions/nope/reject",
                      headers=IDENTITY_HEADERS).status_code == 404


def test_an_agent_may_propose_but_may_not_resolve(monkeypatch):
    """Spec §6: a session (or the loopback caller a Codex session reaches these
    routes as) parks the question and stops. Answering it is a person's — the one
    thing §6 exists to guarantee."""
    calls = _stub_announce(monkeypatch)
    session_headers = {"Tailscale-User-Login": "session:s1"}
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        # proposing is open to every actor class
        proposed = c.post(f"/api/pm/work-items/{wid}/decisions", json={
            "options": OPTIONS, "recommended_key": "pg", "rationale": "two writers"},
            headers=session_headers)
        assert proposed.status_code == 201, proposed.text
        pid = proposed.json()["id"]
        assert proposed.json()["proposedBy"] == "session:s1"

        for path in (f"/api/pm/decisions/{pid}/accept", f"/api/pm/decisions/{pid}/reject"):
            r = c.post(path, json={"chosen_key": "pg"}, headers=session_headers)
            assert r.status_code == 403, f"{path} answered {r.status_code}: {r.text}"
            assert r.json()["detail"]["error"] == "decision_not_permitted"
            assert r.json()["detail"]["message"] == (
                "only a person (or the assistant on their behalf) "
                "may resolve a decision")
        # refused before anything moved, and with no comment posted
        assert _hold_of(c, wid)[0] == "decision" and calls == []
        # an unknown proposal id answers 403 too: the gate runs before the load, so
        # an agent cannot probe which ids exist
        assert c.post("/api/pm/decisions/nope/accept", json={"chosen_key": "pg"},
                      headers=session_headers).status_code == 403
        # the person can still take it
        assert c.post(f"/api/pm/decisions/{pid}/accept", json={"chosen_key": "pg"},
                      headers=IDENTITY_HEADERS).status_code == 200


def test_an_unattributed_loopback_caller_may_not_resolve(monkeypatch):
    """The LAN-open edge is the concrete hole: `lan@local` is agent class, and it
    is how spec §6 has Codex sessions call these routes."""
    _stub_announce(monkeypatch)
    with patch.dict(os.environ, LAN_OPEN), _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        r = c.post(f"/api/pm/decisions/{pid}/accept", json={"chosen_key": "pg"})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "decision_not_permitted"
        assert _hold_of(c, wid)[0] == "decision"


def test_a_real_bound_assistant_token_may_accept_on_a_persons_behalf(monkeypatch):
    """Issue #2006: the ``may_resolve`` assistant carve-out was pinned by a unit
    test on the bare predicate only — nothing drove a REAL drawer token through
    ``IdentityMiddleware`` -> ``_assistant_claims`` -> ``actor_for_request`` ->
    ``may_resolve``. Mint one exactly as the daemon does (``mint_token``, spec
    §4.3) and present it as a bearer header on the real ASGI app; the middleware
    must set ``request.state.via = "assistant"`` from the signed claims alone."""
    from server.assistant.token import mint_token

    calls = _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]

        token = mint_token(IDENTITY_HEADERS["Tailscale-User-Login"], "drawer-session-1")
        acc = c.post(f"/api/pm/decisions/{pid}/accept", json={"chosen_key": "pg"},
                     headers={"Authorization": f"Bearer {token}"})
        assert acc.status_code == 200, acc.text
        assert acc.json()["status"] == "accepted"
        assert acc.json()["resolvedBy"] == (
            f"assistant:{IDENTITY_HEADERS['Tailscale-User-Login']}")
        assert _hold_of(c, wid)[0] is None
        assert calls == [("o/r#1", "Move to Postgres", "concurrent writers",
                          f"assistant:{IDENTITY_HEADERS['Tailscale-User-Login']}")]


def test_a_reject_after_an_accept_is_refused_and_leaves_the_item_moving(monkeypatch):
    """F3's practical shape: the loser of an accept/reject race must re-read the
    row as resolved and answer 409. Were it not refused, the proposal would read
    `accepted` while the reject re-parked the item `owner_action` — a decision
    taken and an issue that never moves."""
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        assert c.post(f"/api/pm/decisions/{pid}/accept", json={"chosen_key": "pg"},
                      headers=IDENTITY_HEADERS).status_code == 200
        late = c.post(f"/api/pm/decisions/{pid}/reject", headers=IDENTITY_HEADERS)
        assert late.status_code == 409 and "already accepted" in late.json()["detail"]["message"]
        assert _hold_of(c, wid)[0] is None


def test_the_routes_audit_the_decision_and_the_hold(monkeypatch):
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")
        pid = _propose(c, wid).json()["id"]
        c.post(f"/api/pm/decisions/{pid}/accept", json={"chosen_key": "pg"},
               headers=IDENTITY_HEADERS)

        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import AuditEvent

        async def _rows():
            async with db_session() as db:
                return [(e.type, e.detail) for e in (await db.execute(
                    select(AuditEvent).order_by(AuditEvent.id))).scalars().all()]
        by_type = {t: d for t, d in c.portal.call(_rows)}
        assert by_type["pm.decision.propose"]["actorClass"] == "person"
        assert by_type["pm.decision.accept"]["chosenKey"] == "pg"
        assert by_type["pm.hold.release"]["signal"] == "decision"


def test_the_overlay_may_not_set_or_clear_a_decision_hold(monkeypatch):
    """Spec §8: decision holds are set only by a proposal and cleared only by
    accepting one — the overlay PATCH is neither, in either direction."""
    _stub_announce(monkeypatch)
    with _client() as c:
        _register(c, "Viberpit", repo="o/r")
        _pid, wid = _seed_one_pending(c, feature_slug="sessions")

        setting = c.patch(f"/api/pm/work-items/{wid}/overlay",
                          json={"holdKind": "decision", "holdReason": "which engine?"},
                          headers=IDENTITY_HEADERS)
        assert setting.status_code == 422
        assert setting.json()["detail"]["error"] == "bad_hold"
        assert setting.json()["detail"]["message"] == \
            "decision holds are set only by a decision proposal"
        assert _hold_of(c, wid)[0] is None

        _propose(c, wid)
        clearing = c.patch(f"/api/pm/work-items/{wid}/overlay",
                           json={"clearHold": True}, headers=IDENTITY_HEADERS)
        assert clearing.status_code == 422
        assert clearing.json()["detail"]["error"] == "bad_hold"
        assert "resolved through its proposal" in clearing.json()["detail"]["message"]
        assert _hold_of(c, wid)[0] == "decision"
