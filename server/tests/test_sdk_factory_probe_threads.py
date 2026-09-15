"""Factory version probes must not block SDK consumers' event loop."""
import asyncio
import inspect
import threading

import pytest

from server.aio import cancel_and_reap
from server.sessions import claude_sdk_factory as sf


@pytest.mark.parametrize('factory,args', [
    (sf.real_sdk_factory, ('/tmp', '/test/config', None, 'default', None)),
    (sf.real_suggestion_factory, ('/tmp', '/test/config', 'account')),
    (sf.real_summary_factory, ('/tmp', '/test/config', 'account')),
])
@pytest.mark.asyncio
async def test_probe_runs_off_loop_but_client_and_auth_stay_on_loop(monkeypatch, factory, args):
    import claude_agent_sdk
    from server.sessions import claude_bin

    loop_thread = threading.get_ident()
    probes = []
    clients = []
    version = [2, 2, 0]
    monkeypatch.setattr(claude_bin, '_min_version', lambda: (2, 1, 0))
    monkeypatch.setattr(claude_bin.shutil, 'which', lambda _: '/test/claude')

    def probe(path):
        probes.append(threading.get_ident())
        return tuple(version)

    def auth(*args):
        assert threading.get_ident() == loop_thread
        return {'CLAUDE_CONFIG_DIR': '/scratch/account', 'CLAUDE_CODE_OAUTH_TOKEN': 'test-token'}

    class Client:
        def __init__(self, options):
            assert threading.get_ident() == loop_thread
            clients.append(options)

    monkeypatch.setattr(claude_bin, '_probe_version', probe)
    monkeypatch.setattr(sf, 'helper_env', auth)
    monkeypatch.setattr(claude_agent_sdk, 'ClaudeSDKClient', Client)
    for _ in range(2):
        result = factory(*args)
        if inspect.isawaitable(result):
            await result
    assert len(clients) == 2
    assert len(probes) == 2 and all(t != loop_thread for t in probes)
    assert all(opts.cli_path == '/test/claude' for opts in clients)
    version[:] = [2, 0, 0]
    await factory(*args)
    assert len(probes) == 3  # No cache conceals a changed version/floor.
    assert clients[-1].cli_path is None


@pytest.mark.asyncio
async def test_cancel_during_probe_does_not_construct_client(monkeypatch):
    import claude_agent_sdk

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    clients = []

    def probe():
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        return {}

    monkeypatch.setattr(sf, '_cli_path_kwargs', probe)
    monkeypatch.setattr(claude_agent_sdk, 'ClaudeSDKClient', lambda **kw: clients.append(kw))
    task = asyncio.create_task(sf.real_sdk_factory('/tmp', '/test/config', None, 'default', None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await cancel_and_reap(task, phase="test.factory-probe")
        assert task.cancelled()
        assert clients == []
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('service_name,factory_name,answer,expected', [
    ('suggestions', 'real_suggestion_factory', '["Run tests"]', ['Run tests']),
    ('summaries', 'real_summary_factory', 'Running tests', 'Running tests'),
])
async def test_services_await_real_factory(monkeypatch, service_name, factory_name, answer, expected):
    import claude_agent_sdk
    from server.sessions.suggestions import SuggestionService
    from server.sessions.summaries import SummaryService
    from server.tests.fakes import fake_sdk as f

    loop_thread = threading.get_ident()
    probes = []
    lifecycle = []

    def probe():
        probes.append(threading.get_ident())
        return {'cli_path': '/test/claude'}

    class Client(f.FakeSDKClient):
        def __init__(self, options):
            assert threading.get_ident() == loop_thread
            super().__init__([f.stream_start(), f.stream_text(answer), f.result()])

        async def __aenter__(self):
            lifecycle.append(('enter', threading.get_ident()))
            return await super().__aenter__()

        async def __aexit__(self, *args):
            lifecycle.append(('exit', threading.get_ident()))
            return await super().__aexit__(*args)

    monkeypatch.setattr(sf, '_cli_path_kwargs', probe)
    monkeypatch.setattr(sf, 'helper_env', lambda *args: {})
    monkeypatch.setattr(claude_agent_sdk, 'ClaudeSDKClient', Client)
    cls = SuggestionService if service_name == 'suggestions' else SummaryService
    result = await cls(getattr(sf, factory_name)).generate('/tmp', '/test/config', 'context', 'account')
    assert result == expected
    assert probes and all(t != loop_thread for t in probes)
    assert lifecycle == [('enter', loop_thread), ('exit', loop_thread)]


@pytest.mark.asyncio
async def test_driver_start_and_reconnect_await_factory(monkeypatch):
    import claude_agent_sdk
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    from server.tests.fakes.fake_sdk import FakeSDKClient

    loop_thread = threading.get_ident()
    probes = []
    options = []
    lifecycle = []

    def probe():
        probes.append(threading.get_ident())
        return {'cli_path': '/test/claude'}

    class Client(FakeSDKClient):
        def __init__(self, options):
            assert threading.get_ident() == loop_thread
            super().__init__([])
            self.options = options

        async def __aenter__(self):
            options.append(self.options)
            lifecycle.append(('enter', threading.get_ident()))
            return await super().__aenter__()

        async def __aexit__(self, *args):
            lifecycle.append(('exit', threading.get_ident()))
            return await super().__aexit__(*args)

    monkeypatch.setattr(sf, '_cli_path_kwargs', probe)
    monkeypatch.setattr(claude_agent_sdk, 'ClaudeSDKClient', Client)
    driver = ClaudeAgentDriver('test', 'account', '/tmp', '/test/config', mode='default',
                               sdk_factory=sf.real_sdk_factory, registry=PermissionRegistry(),
                               identity='test')
    try:
        await driver.start()
        await driver.reconnect()
    finally:
        await driver.aclose()
    assert len(probes) == 2 and all(t != loop_thread for t in probes)
    assert len(options) == 2
    assert all(o.cli_path == '/test/claude' for o in options)
    assert lifecycle == [('enter', loop_thread), ('exit', loop_thread)] * 2


@pytest.mark.asyncio
async def test_cancelled_factory_never_becomes_driver_client():
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry

    entered = asyncio.Event()

    async def factory(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    driver = ClaudeAgentDriver('test', 'account', '/tmp', '/test/config', mode='default',
                               sdk_factory=factory, registry=PermissionRegistry(), identity='test')
    task = asyncio.create_task(driver.start())
    await entered.wait()
    await cancel_and_reap(task, phase='test.driver-factory')
    assert task.cancelled()
    assert driver._client is None
    await driver.aclose()
