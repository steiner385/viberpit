"""Tests for the reconciler kill switch config setting (003 Phase B)."""
from server.config import Settings


def test_reconcile_kill_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("CCSWITCH_GH_RECONCILE_ENABLED", raising=False)
    assert Settings().gh_reconcile_enabled is False


def test_reconcile_kill_switch_reads_env(monkeypatch):
    monkeypatch.setenv("CCSWITCH_GH_RECONCILE_ENABLED", "true")
    assert Settings().gh_reconcile_enabled is True
