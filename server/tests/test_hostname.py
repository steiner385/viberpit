def test_env_wins(monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOST_NAME", " rocky ")
    from server.hostname import self_host_name
    assert self_host_name() == "rocky"


def test_falls_back_to_first_hostname_label(monkeypatch):
    import socket
    monkeypatch.delenv("CCSWITCH_HOST_NAME", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "dobby.tail0dd570.ts.net")
    from server.hostname import self_host_name
    assert self_host_name() == "dobby"


def test_orchestrator_reexports_the_same_function():
    from server import hostname
    from server.sessions import orchestrator
    assert orchestrator.self_host_name is hostname.self_host_name
