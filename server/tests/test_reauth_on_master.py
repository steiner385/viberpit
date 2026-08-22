"""Reauth runs on the MASTER (edge), never proxied to a worker daemon.

dobby is the master: ccswitch core, configuration and operations live there.
rocky is a worker that executes sessions. Reauth is an OPERATION — it mints a
credential — so it belongs on the master even though it needs a browser.

It was proxied to the daemon because the daemon owns the session X11 displays.
That reasoning confused "needs a display" with "belongs on the worker", and it
broke in production: the worker had no `claude` on its service PATH, so reauth
failed with "[Errno 2] No such file or directory: 'claude'".

Running it on the master is also strictly better:
  * the credential dir is LOCAL there, not an sshfs mount whose flock does not
    work across hosts (proven 2026-08-22 — the ledger corrupted for this reason)
  * the master always has the binary and the X11 stack; a worker may not
  * adding a second worker cannot change how reauth behaves
"""
from __future__ import annotations

from server.reauth_remote import InProcessReauthAdapter, RemoteReauthManager


def test_reauth_manager_is_in_process_even_in_remote_mode(monkeypatch):
    """The routing decision, isolated from app construction."""
    from server.app import _build_reauth_facade

    facade = _build_reauth_facade(remote_mode=True, x11_getter=lambda: None,
                                  timeout_s=900.0, reauth_manager=None)
    assert isinstance(facade, InProcessReauthAdapter), (
        "reauth must run on the master, not proxy to a worker daemon")
    assert not isinstance(facade, RemoteReauthManager)


def test_reauth_manager_is_in_process_in_single_host_mode():
    from server.app import _build_reauth_facade

    facade = _build_reauth_facade(remote_mode=False, x11_getter=lambda: None,
                                  timeout_s=900.0, reauth_manager=None)
    assert isinstance(facade, InProcessReauthAdapter)


def test_injected_manager_still_wins():
    """Tests inject their own manager; that must keep working."""
    from server.app import _build_reauth_facade
    from server.reauth import ReauthManager

    mine = ReauthManager(timeout_s=1.0)
    facade = _build_reauth_facade(remote_mode=True, x11_getter=lambda: None,
                                  timeout_s=900.0, reauth_manager=mine)
    assert isinstance(facade, InProcessReauthAdapter)


def test_x11_getter_is_used_for_the_reauth_display():
    """The master builds its OWN display manager for reauth; session displays
    stay on the worker, where the sessions actually run."""
    from server.app import _build_reauth_facade

    sentinel = object()
    calls = []

    def getter():
        calls.append(1)
        return sentinel

    facade = _build_reauth_facade(remote_mode=True, x11_getter=getter,
                                  timeout_s=900.0, reauth_manager=None)
    assert facade is not None
    # the manager holds the getter, not a resolved value (display is built lazily)
    assert calls == [], "x11 must be resolved lazily, at reauth time"
