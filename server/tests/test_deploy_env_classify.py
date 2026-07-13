"""Tests for the canonical deploy-environment classifier (server.github.deploy_envs).

The classifier maps a raw GitHub deployment-environment name to one of the three
metric buckets — qa / prod / admin — or None when it matches nothing. It is the
single source of truth for "which category is this env" across the corrected
``deployed`` bool (pr_status) and the per-repo deploy backlog summary.
"""
from server.github.deploy_envs import classify_env


def test_exact_production_is_prod():
    # steiner385/ccswitch names its env exactly "production" — must stay prod.
    assert classify_env("production") == "prod"


def test_namespaced_production_is_prod():
    # cairnea/KinDash namespaces envs — this is the bug the fix targets.
    assert classify_env("kindash / production") == "prod"


def test_namespaced_qa_is_qa():
    assert classify_env("kindash / qa") == "qa"


def test_admin_wins_over_production():
    # admin is its own bucket; admin-production must classify as admin, NOT prod.
    assert classify_env("kindash / admin-production") == "admin"


def test_admin_qa_is_admin():
    assert classify_env("kindash / admin-qa") == "admin"


def test_bare_admin_is_admin():
    assert classify_env("kindash / admin") == "admin"


def test_staging_is_qa():
    assert classify_env("staging") == "qa"


def test_unknown_env_is_none():
    assert classify_env("preview-123") is None
    assert classify_env("") is None


def test_case_and_whitespace_insensitive():
    assert classify_env("  Kindash / Production  ") == "prod"
    assert classify_env("PROD") == "prod"


def test_prod_token_matches():
    # a bare "prod" token (not the full word "production") still counts.
    assert classify_env("app / prod") == "prod"
