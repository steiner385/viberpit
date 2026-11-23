"""Unit tests for the in-memory pr-doctor job registry (issue #668).

The store tracks the async manual-trigger pipeline (clone → assess → spawn) so
the UI can poll a job id instead of blocking the edge→daemon RPC for 12–41 s.
"""
from __future__ import annotations

from server.automations.pr_doctor_jobs import JobStore, finalize, outcome_message


def test_create_returns_queued_job_with_unique_id():
    store = JobStore()
    a = store.create(target="o/r#1", kind="failing_ci")
    b = store.create(target="o/r#2", kind="merge_conflict")
    assert a.status == "queued" and a.phase == "queued"
    assert a.job_id != b.job_id
    assert a.job_id.startswith("prd-")
    assert store.get(a.job_id) is a
    assert store.get("prd-missing") is None


def test_advance_marks_phase_and_running():
    store = JobStore()
    job = store.create(target="o/r#1", kind="failing_ci")
    store.advance(job, "cloning")
    assert (job.status, job.phase) == ("running", "cloning")
    store.advance(job, "assessing")
    assert job.phase == "assessing"


def test_finalize_ready_from_spawned():
    job = JobStore().create(target="o/r#1", kind="failing_ci")
    finalize(job, "spawned", level="ok", message="Spawned fix session", session_id="s-1")
    assert (job.status, job.phase, job.level) == ("ready", "ready", "ok")
    assert job.session_id == "s-1"
    assert job.error_phase is None


def test_finalize_deferred_from_ceiling():
    job = JobStore().create(target="o/r#1", kind="failing_ci")
    finalize(job, "ceiling", level="warn", message="usage ceiling")
    assert job.status == "deferred" and job.level == "warn"
    assert job.error_phase is None


def test_finalize_error_records_failed_phase():
    job = JobStore().create(target="o/r#1", kind="failing_ci")
    finalize(job, "clone_failed", level="error", message="Clone failed")
    assert (job.status, job.error_phase) == ("error", "cloning")

    job2 = JobStore().create(target="o/r#2", kind="failing_ci")
    finalize(job2, "spawn_failed", level="error", message="Spawn failed")
    assert (job2.status, job2.error_phase) == ("error", "spawning")


def test_store_evicts_oldest_beyond_cap():
    store = JobStore(cap=3)
    ids = [store.create(target=f"o/r#{i}", kind="failing_ci").job_id for i in range(5)]
    # Oldest two evicted; newest three retained.
    assert store.get(ids[0]) is None
    assert store.get(ids[1]) is None
    assert all(store.get(i) is not None for i in ids[2:])


def test_to_dict_is_json_safe():
    job = JobStore().create(target="o/r#1", kind="failing_ci")
    d = job.to_dict()
    assert d["job_id"] == job.job_id
    assert d["target"] == "o/r#1"
    assert d["status"] == "queued"
    assert d["session_id"] is None
    assert set(d) >= {"job_id", "target", "kind", "status", "phase",
                      "session_id", "error_phase", "message", "level"}


# --- #3429: settled jobs expire; the reason is not repeated -------------------

def test_settled_job_expires_after_ttl():
    store = JobStore(ttl_s=60)
    job = store.create(target="o/r#1", kind="merge_conflict")
    finalize(job, "untrusted_author", level="warn", message="Not fixing")
    assert store.get(job.job_id) is job
    job.settled_at -= 61  # settled a minute ago
    assert store.get(job.job_id) is None
    # Gone for good, not merely hidden.
    job.settled_at += 61
    assert store.get(job.job_id) is None


def test_running_job_never_expires():
    store = JobStore(ttl_s=0)
    job = store.create(target="o/r#1", kind="failing_ci")
    store.advance(job, "cloning")
    assert store.get(job.job_id) is job


def test_create_prunes_expired_settled_jobs():
    store = JobStore(ttl_s=60)
    old = store.create(target="o/r#1", kind="merge_conflict")
    finalize(old, "spawn_failed", level="error", message="Spawn failed")
    old.settled_at -= 61
    store.create(target="o/r#2", kind="merge_conflict")
    assert old.job_id not in store._jobs


def test_outcome_message_appends_a_distinct_reason():
    assert outcome_message("Session busy — try again shortly", "claimed by sess-1") == (
        "Session busy — try again shortly — claimed by sess-1")
    assert outcome_message("Spawned fix session", None) == "Spawned fix session"


def test_outcome_message_drops_a_reason_the_label_already_says():
    # The live #3429 toast: "…not a trusted author — the PR's author is not a trusted author".
    assert outcome_message("Not fixing: the PR's author is not a trusted author",
                           "the PR's author is not a trusted author") == (
        "Not fixing: the PR's author is not a trusted author")
