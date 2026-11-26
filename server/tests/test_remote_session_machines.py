"""Remote sessions' machine names (``server/remote_sessions/machines.py``): which Remote
Control machine ids a host's config files carry, how the fleet's reports merge, the
operator's labels, and the precedence a listed session's ``machineName`` follows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.remote_sessions import machines


def _claude_json(path: Path, **data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "store"))
    return home, tmp_path / "store" / "cfg"


def test_local_ids_come_from_home_and_every_account_config_dir(fake_home) -> None:
    home, cfg = fake_home
    _claude_json(home / ".claude.json", remoteControlMachineId="m-home", machineID="not-it")
    _claude_json(cfg / "alice" / ".claude.json", remoteControlMachineId="m-alice")
    _claude_json(cfg / "bob" / ".claude.json", machineID="only-the-other-key")
    _claude_json(cfg / "carol" / ".claude.json", remoteControlMachineId="m-home")
    (cfg / "dave").mkdir(parents=True)  # no .claude.json at all
    (cfg / "erin").mkdir()
    (cfg / "erin" / ".claude.json").write_text("{not json")
    _claude_json(cfg / "frank" / ".claude.json", remoteControlMachineId=12)
    _claude_json(cfg / "gina" / ".claude.json", remoteControlMachineId="../bad id")
    (cfg / "stray-file").write_text("x")

    # The home file is the stronger source even when an account dir repeats the id.
    assert machines.local_machine_ids() == {"m-home": "home", "m-alice": "account"}


def test_local_ids_tolerate_a_missing_store_and_home(fake_home) -> None:
    assert machines.local_machine_ids() == {}


def test_local_payload_names_this_host(fake_home, monkeypatch) -> None:
    home, _cfg = fake_home
    _claude_json(home / ".claude.json", remoteControlMachineId="m-home")
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    assert machines.local_payload() == {"host": "dobby", "machineIds": {"m-home": "home"}}


def test_merge_prefers_a_hosts_own_home_over_a_shared_account_dir() -> None:
    merged = machines.merge_hosts([
        ("dobby", {"machineIds": {"m-1": "home", "m-s": "account", "m-t": "account"}}),
        ("rocky", {"machineIds": {"m-1": "account", "m-s": "account", "m-r": "home"}}),
        ("thready", {"machineIds": {"m-t": "home"}}),
        ("broken", None), ("worse", {"machineIds": ["m-x"]}), ("", {"machineIds": {"m-y": "home"}}),
        ("odd", {"machineIds": {"../x": "home", 7: "home"}}),
    ])
    assert merged == {"m-1": "dobby", "m-s": "dobby / rocky", "m-r": "rocky",
                      "m-t": "thready"}


def test_labels_patch_sets_trims_removes_and_validates() -> None:
    cur = machines.stored_labels({"m-a": "Laptop", "m-b": "Desk", "../x": "bad", "m-c": 7,
                                  "m-d": "  "})
    assert cur == {"m-a": "Laptop", "m-b": "Desk"}
    assert machines.stored_labels(None) == {} and machines.stored_labels("x") == {}
    out = machines.apply_labels(cur, {"m-a": "  Work   laptop ", "m-b": None, "m-new": "",
                                      "m-z": "Mac mini"})
    assert out == {"m-a": "Work laptop", "m-z": "Mac mini"}
    for bad in ({"../x": "a"}, {"m-a": "x" * (machines.MAX_LABEL_CHARS + 1)}, {"m-a": 7}):
        with pytest.raises(ValueError):
            machines.apply_labels(cur, bad)
    with pytest.raises(ValueError):
        machines.apply_labels({}, {f"m-{i}": "x" for i in range(machines.MAX_LABELS + 1)})


def test_machine_name_is_host_then_label_then_none() -> None:
    sessions = [{"id": "s1", "machineId": "m-host"}, {"id": "s2", "machineId": "m-label"},
                {"id": "s3", "machineId": "m-unknown"}, {"id": "s4", "machineId": None},
                "garbage"]
    named = machines.apply_names(sessions, {"m-host": "dobby"},
                                 {"m-host": "ignored", "m-label": "Laptop"})
    assert [(s["id"], s["machineName"]) for s in named] == [
        ("s1", "dobby"), ("s2", "Laptop"), ("s3", None), ("s4", None)]
