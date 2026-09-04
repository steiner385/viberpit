"""session_bus_env(): give a service-launched Chrome a route to gnome-keyring.

ccswitch.service has no DBUS_SESSION_BUS_ADDRESS, so Chrome cannot fetch the
"Chrome Safe Storage" key and silently drops every v11 cookie copied from the
real profile (spec 2026-09-04-reauth-profile-fidelity-design.md).
"""
import socket

from ccswitch.auth_login import session_bus_env


def _bus(tmp_path, uid=1000):
    runtime = tmp_path / str(uid)
    runtime.mkdir()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    return runtime, s


def test_injects_bus_and_runtime_dir_when_socket_exists(tmp_path):
    runtime, s = _bus(tmp_path)
    try:
        out = session_bus_env({}, uid=1000, run_root=str(tmp_path))
    finally:
        s.close()
    assert out == {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        "XDG_RUNTIME_DIR": str(runtime),
    }


def test_empty_when_no_socket(tmp_path):
    assert session_bus_env({}, uid=1000, run_root=str(tmp_path)) == {}


def test_plain_file_named_bus_is_not_a_socket(tmp_path):
    runtime = tmp_path / "1000"
    runtime.mkdir()
    (runtime / "bus").write_text("")
    assert session_bus_env({}, uid=1000, run_root=str(tmp_path)) == {}


def test_does_not_override_existing_values(tmp_path):
    runtime, s = _bus(tmp_path)
    try:
        out = session_bus_env(
            {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/elsewhere", "XDG_RUNTIME_DIR": str(runtime)},
            uid=1000, run_root=str(tmp_path),
        )
    finally:
        s.close()
    assert out == {}


def test_honours_xdg_runtime_dir_from_environ(tmp_path):
    runtime, s = _bus(tmp_path, uid=4242)
    try:
        out = session_bus_env({"XDG_RUNTIME_DIR": str(runtime)}, uid=1000, run_root="/nonexistent")
    finally:
        s.close()
    assert out == {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}"}
