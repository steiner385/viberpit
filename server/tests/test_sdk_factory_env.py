from server.sessions import sdk_factory


def test_extra_env_merged(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["env"] = options.env

    class FakeOpts:
        def __init__(self, **kw):
            self.env = kw["env"]

    monkeypatch.setattr(
        sdk_factory, "_import_sdk", lambda: (FakeClient, FakeOpts), raising=False
    )
    sdk_factory.real_sdk_factory(
        "/tmp",
        "/cfg",
        None,
        "interactive",
        None,
        extra_env={"DISPLAY": ":100", "XAUTHORITY": "/x"},
    )
    assert captured["env"]["DISPLAY"] == ":100"
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/cfg"
