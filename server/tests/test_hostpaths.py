# server/tests/test_hostpaths.py
"""Host paths were spelled in four modules; Phase 3 renames them on every host, so
they must come from one place and, until then, equal the literals the units use."""
from ccswitch import brand
from server import hostpaths
from server.deploy import serve
from server.ipc import paths as ipc_paths
from server.sessions import daemon_health, deploy_snapshot


def test_values_match_the_installed_units():
    # deploy/*.service: RuntimeDirectory=ccswitch, StateDirectory=ccswitch,
    # EnvironmentFile=/etc/ccswitch/ccswitch.env
    assert hostpaths.SESSIOND_SOCKET == "/run/ccswitch/sessiond.sock"
    assert hostpaths.DEPLOY_SOCKET == "/run/ccswitch/deploy.sock"
    assert hostpaths.DEPLOY_STATUS_PATH == "/run/ccswitch/deploy-status.json"
    assert hostpaths.HEALTH_STATE_DIR == "/var/lib/ccswitch/health"
    assert hostpaths.DEPLOY_SNAPSHOT_PATH == "/var/lib/ccswitch/deploy-snapshot/live.json"
    assert hostpaths.LOG_DIR == "/var/log/ccswitch"
    assert hostpaths.ENV_FILE == "/etc/ccswitch/ccswitch.env"


def test_derived_from_host_slug():
    for value in (hostpaths.RUN_DIR, hostpaths.STATE_DIR, hostpaths.LOG_DIR, hostpaths.ETC_DIR):
        assert value.endswith("/" + brand.HOST_SLUG), value


def test_consumers_alias_hostpaths():
    assert ipc_paths.DEFAULT_SOCKET_PATH is hostpaths.SESSIOND_SOCKET
    assert serve.DEFAULT_DEPLOY_SOCKET is hostpaths.DEPLOY_SOCKET
    assert serve.DEFAULT_DEPLOY_STATUS_PATH is hostpaths.DEPLOY_STATUS_PATH
    assert deploy_snapshot.DEFAULT_PATH is hostpaths.DEPLOY_SNAPSHOT_PATH
    assert daemon_health._DEFAULT_HEALTH_STATE_DIR is hostpaths.HEALTH_STATE_DIR
