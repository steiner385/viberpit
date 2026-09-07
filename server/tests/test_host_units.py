"""Host service selection uses the target host, including mixed-version fleets."""
import subprocess

from server.host_units import resolve_unit


def test_prefers_new_and_returns_canonical_id():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'LoadState=loaded\nId=viberpit-daemon.service\n')
    assert resolve_unit('daemon', run=run) == 'viberpit-daemon.service'
    assert len(calls) == 1


def test_falls_back_on_remote_host():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        raw = 'LoadState=not-found\n' if argv[-1].startswith('viberpit') else 'LoadState=loaded\nId=ccswitch-daemon.service\n'
        return subprocess.CompletedProcess(argv, 0, raw)
    prefix = ['ssh', '-oBatchMode=yes', 'rocky']
    assert resolve_unit('daemon', run=run, prefix=prefix) == 'ccswitch-daemon.service'
    assert len(calls) == 2
    assert all(c[:3] == prefix for c in calls)


def test_new_default_when_absent():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, 'LoadState=not-found\n')
    assert resolve_unit('edge', run=run) == 'viberpit.service'


def test_new_alias_resolves_legacy_canonical_identity():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, 'Id=ccswitch-daemon.service\nLoadState=loaded\n')
    assert resolve_unit('daemon', run=run) == 'ccswitch-daemon.service'


def test_installed_systemctl_offline_alias_contract(tmp_path):
    """The real binary recognizes the backward alias without a running manager."""
    import shutil
    import pytest
    if not shutil.which('systemctl'):
        pytest.skip('systemctl is not installed')
    units = tmp_path / 'etc/systemd/system'
    units.mkdir(parents=True)
    (units / 'viberpit-daemon.service').write_text(
        '[Unit]\nDescription=Offline fixture\n[Service]\nExecStart=/bin/true\n'
        '[Install]\nWantedBy=multi-user.target\nAlias=ccswitch-daemon.service\n')
    subprocess.run(['systemctl', '--root', str(tmp_path), 'enable',
                    'viberpit-daemon.service'], check=True, capture_output=True, text=True)
    assert (units / 'ccswitch-daemon.service').is_symlink()
    for unit in ('viberpit-daemon.service', 'ccswitch-daemon.service'):
        result = subprocess.run(['systemctl', '--root', str(tmp_path), 'is-enabled', unit],
                                check=True, capture_output=True, text=True)
        assert result.stdout.strip() in {'enabled', 'alias'}


def test_remote_restart_resolves_on_remote_only():
    import asyncio
    from server.deploy.runners import RunnerDeploy, RunnerTarget
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ['ssh', '-oBatchMode=yes', '-oConnectTimeout=10', 'rocky']
        output = 'Id=ccswitch-daemon.service\nLoadState=loaded\n'
        if 'show' in argv and argv[-1] == 'viberpit-daemon.service':
            output = 'LoadState=not-found\n'
        return subprocess.CompletedProcess(argv, 0, output, '')
    target = RunnerTarget('rocky', 'https://rocky:9443', True, True, 'rocky', '/srv/viberpit')
    runner = RunnerDeploy(target, local_name='dobby', run=run)
    asyncio.run(runner._restart(local=False))
    assert calls[-1][4:] == ['sudo', '-n', 'systemctl', 'restart', 'ccswitch-daemon.service']
    assert len(calls) == 3
