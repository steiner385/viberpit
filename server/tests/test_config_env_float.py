"""Review Fix 4: ONE env-float parser, with a clamp.

`_env_float` had been copy-pasted into three modules (orchestrator, replay,
deploy/drain) and had already drifted — drain's copy caught only ValueError.
Constitution VI: one source of truth. These tests pin the shared contract so a
future fourth copy has nothing to justify itself with.
"""
import pytest

from server.config import env_float


def test_missing_returns_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_TEST_FLOAT", raising=False)
    assert env_float("CCSWITCH_TEST_FLOAT", 7.5) == 7.5


def test_valid_value_is_honoured(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", "2.5")
    assert env_float("CCSWITCH_TEST_FLOAT", 7.5) == 2.5


@pytest.mark.parametrize("bad", ["", "abc", "1,5", "10s", " "])
def test_malformed_falls_back_to_default_instead_of_crashing(monkeypatch, bad):
    """A typo'd override must never crash daemon startup."""
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", bad)
    assert env_float("CCSWITCH_TEST_FLOAT", 7.5) == 7.5


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_falls_back_to_default(monkeypatch, bad):
    """float() accepts these, but a NaN/inf timeout is nonsense downstream."""
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", bad)
    assert env_float("CCSWITCH_TEST_FLOAT", 7.5) == 7.5


def test_below_minimum_falls_back_to_default(monkeypatch):
    """The whole point of the clamp: a 0 ceiling would make wait_for fire
    instantly and turn every transcript into an empty one."""
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", "0")
    assert env_float("CCSWITCH_TEST_FLOAT", 10.0, minimum=0.1) == 10.0
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", "-5")
    assert env_float("CCSWITCH_TEST_FLOAT", 10.0, minimum=0.1) == 10.0


def test_at_minimum_is_accepted(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", "0.1")
    assert env_float("CCSWITCH_TEST_FLOAT", 10.0, minimum=0.1) == 0.1


def test_negative_allowed_when_no_minimum_given(monkeypatch):
    monkeypatch.setenv("CCSWITCH_TEST_FLOAT", "-5")
    assert env_float("CCSWITCH_TEST_FLOAT", 10.0) == -5.0


def test_no_module_keeps_a_private_copy():
    """The anti-pattern this fix removed must not come back."""
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-n", "def _env_float", "--", "server", "src"],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"a local _env_float copy is back:\n{out.stdout}"
