# server/tests/test_hostpaths.py
"""Host paths were spelled in four modules; Phase 3 renames them on every host, so
they must come from one place and, until then, equal the literals the units use."""
from ccswitch import brand
from server import hostpaths
from server.deploy import serve
from server.ipc import paths as ipc_paths
from server.sessions import daemon_health, deploy_snapshot


def test_paths_prefer_new_then_legacy_then_new_default(tmp_path):
    new, old = tmp_path / brand.HOST_SLUG, tmp_path / brand.LEGACY_SLUG
    def choose():
        return hostpaths.prefer_existing(str(new), str(old))
    assert choose() == str(new)
    old.mkdir()
    assert choose() == str(old)
    new.symlink_to(old, target_is_directory=True)
    assert choose() == str(new)


def test_derived_paths_share_selected_roots():
    assert hostpaths.SESSIOND_SOCKET == f"{hostpaths.RUN_DIR}/sessiond.sock"
    assert hostpaths.DEPLOY_SOCKET == f"{hostpaths.RUN_DIR}/deploy.sock"
    assert hostpaths.DEPLOY_STATUS_PATH == f"{hostpaths.RUN_DIR}/deploy-status.json"
    assert hostpaths.HEALTH_STATE_DIR == f"{hostpaths.STATE_DIR}/health"
    assert hostpaths.DEPLOY_SNAPSHOT_PATH == f"{hostpaths.STATE_DIR}/deploy-snapshot/live.json"


def test_consumers_alias_hostpaths():
    assert ipc_paths.DEFAULT_SOCKET_PATH is hostpaths.SESSIOND_SOCKET
    assert serve.DEFAULT_DEPLOY_SOCKET is hostpaths.DEPLOY_SOCKET
    assert serve.DEFAULT_DEPLOY_STATUS_PATH is hostpaths.DEPLOY_STATUS_PATH
    assert deploy_snapshot.DEFAULT_PATH is hostpaths.DEPLOY_SNAPSHOT_PATH
    assert daemon_health._DEFAULT_HEALTH_STATE_DIR is hostpaths.HEALTH_STATE_DIR
