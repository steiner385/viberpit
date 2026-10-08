"""Offline tests: these never invoke a provider or native engine."""
import json
from pathlib import Path

import pytest

from scripts import framework_guidance_eval as fg

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    from server.sessions import helper_call, account_auth, adopt
    from viberpit import accounts, paths
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: '/fake/account')
    monkeypatch.setattr(helper_call, 'is_account_helper_ready', lambda name: True)
    monkeypatch.setattr(accounts, 'runner_uses_envelope', lambda name: False)
    monkeypatch.setattr(accounts, 'list_accounts', lambda: [{'name': 'account'}])
    monkeypatch.setattr(adopt, '_map_account', lambda *a: 'account')
    monkeypatch.setattr(account_auth, 'oauth_env_for', lambda account: {'CLAUDE_CODE_OAUTH_TOKEN': 'test-subscription-token'})
    monkeypatch.setattr(accounts, 'provider_or_none', lambda name: 'anthropic')
    monkeypatch.setattr(paths, 'account_cfg_dir', lambda name: Path('/fake') / name)
    monkeypatch.setattr(account_auth, 'helper_env', lambda *a, **k: {
        **__import__('os').environ, 'CLAUDE_CONFIG_DIR': '/fake/scratch',
        'CLAUDE_CODE_OAUTH_TOKEN': 'test-subscription-token'})



def test_missing_account_never_starts_worker(monkeypatch, tmp_path):
    from server.sessions import helper_call
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: None)
    monkeypatch.setattr(fg, 'worker_call', lambda *_: pytest.fail('worker started'))
    report = fg.run_comparison(ROOT, model='claude-sonnet-4-20250514', budget=1, timeout=30)
    assert report['executed'] is False
    assert report['status'] == 'missing-subscription-account'


@pytest.mark.parametrize('budget', [0, -1, float('nan'), float('inf'), 2.01])
def test_budget_bounds(budget):
    with pytest.raises(ValueError):
        fg.validate_request('claude-sonnet-4-20250514', budget, 30)


@pytest.mark.parametrize('model', ['', 'sonnet', 'claude-sonnet-latest'])
def test_model_must_be_exact(model):
    with pytest.raises(ValueError):
        fg.validate_request(model, 1, 30)


def test_worker_environment_has_one_authority(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'dedicated-test-key')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://untrusted.invalid')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'not-allowed')
    monkeypatch.setenv('NODE_OPTIONS', '--require malicious.js')
    monkeypatch.setenv('VIBERPIT_CLAUDE_BIN', '/untrusted')
    env = fg.worker_environment(None)
    assert env['CLAUDE_CODE_OAUTH_TOKEN'] == 'test-subscription-token'
    assert not ({'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'NODE_OPTIONS',
                 'VIBERPIT_CLAUDE_BIN', 'PYTHONPATH'} & env.keys())


def test_corpus_and_paired_prompts():
    corpus, guidance, provenance = fg.load_inputs(ROOT)
    assert len(corpus) == 3
    assert provenance['guidance_sha256']
    baseline = fg.make_prompt(corpus[1]['html'], corpus[1]['vocabulary'], '')
    treatment = fg.make_prompt(corpus[1]['html'], corpus[1]['vocabulary'], guidance)
    assert corpus[1]['html'] in baseline and corpus[1]['html'] in treatment
    assert baseline.split('REFERENCE DATA')[0] == treatment.split('REFERENCE DATA')[0]
    assert 'fixtures/mobile-overflow.html' not in baseline
    assert 'expected_failures' not in baseline


def test_score_clean_false_positive():
    assert fg.score(['mobile-overflow'], []) == {'true_positives': 0, 'false_positives': 1,
                                               'false_negatives': 0}


def test_output_schema():
    assert fg.parse_findings('{"findings":[]}', ['mobile-overflow']) == []
    for text in ('', '{}', '{"findings":["unknown"]}', '{"findings":[],"extra":1}',
                 '{"findings":["mobile-overflow","mobile-overflow"]}'):
        with pytest.raises(ValueError):
            fg.parse_findings(text, ['mobile-overflow'])


def test_single_json_fence_from_native_response():
    assert fg.parse_findings('```json\n{"findings":["mobile-overflow"]}\n```',
                             ['mobile-overflow']) == ['mobile-overflow']
    assert fg.parse_findings(' \n```json\n{"findings":[]}\n```\n', []) == []


@pytest.mark.parametrize('text', [
    'Here are findings:\n```json\n{"findings":[]}\n```',
    '```json\n{"findings":[]}\n```\nDone.',
    '```json\n{"findings":[]}\n```\n```json\n{"findings":[]}\n```',
    '```javascript\n{"findings":[]}\n```',
    '```\n{"findings":[]}\n```',
    '```json {"findings":[]} ```',
    '```json\n{}\n```',
    '```json\n{"findings":["unknown"]}\n```',
    '```json\n{"findings":[],"extra":1}\n```',
    '```json\n{"findings":["mobile-overflow","mobile-overflow"]}\n```',
])
def test_fenced_response_preserves_strict_validation(text):
    with pytest.raises(ValueError):
        fg.parse_findings(text, ['mobile-overflow'])


def test_unavailable_key_report_is_private(tmp_path, monkeypatch):
    from server.sessions import helper_call
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: None)
    path = tmp_path / 'report.json'
    with fg.reserve_report(path) as output:
        fg.write_report(output, fg.run_comparison(ROOT, model='claude-sonnet-4-20250514', budget=1, timeout=30))
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['executed'] is False


MODEL = 'claude-sonnet-4-20250514'


def options():
    return fg.build_options('/verified/claude', MODEL, .1, '/scratch/work', '/scratch/config')


def messages(*, tools=None, init=True, result=True, cost=.01, error=False, text='{"findings":[]}'):
    from claude_agent_sdk import ResultMessage, SystemMessage
    values = []
    if init:
        values.append(SystemMessage('init', {'tools': tools or [], 'model': MODEL, 'mcp_servers': [], 'slash_commands': [], 'skills': []}))
    if result:
        values.append(ResultMessage('success', 1, 1, error, 1, 'private-id',
                                    total_cost_usd=cost, result=text, usage={'input_tokens': 10}))
    return values


@pytest.mark.asyncio
async def test_successful_query_checks_observed_capabilities():
    async def query(**kwargs):
        for msg in messages():
            yield msg
    result = await fg.consume_query('synthetic', options(), [], query)
    assert result['findings'] == []
    assert result['cost_usd'] == .01
    assert result['usage'] == {'input_tokens': 10}
    assert result['observed_model'] == MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [dict(init=False), dict(result=False), dict(tools=['Bash']),
                                     dict(cost=None), dict(cost=.1), dict(cost=float('nan')),
                                     dict(error=True), dict(text=''), dict(text='not json')])
async def test_unexecuted_failed_or_unbounded_stream_rejected(kwargs):
    attempts = 0
    async def query(**ignored):
        nonlocal attempts
        attempts += 1
        for msg in messages(**kwargs):
            yield msg
    with pytest.raises(ValueError):
        await fg.consume_query('synthetic', options(), [], query)
    assert attempts == 1


@pytest.mark.asyncio
async def test_tool_attempt_rejected_and_stream_closed():
    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    closed = False
    async def query(**ignored):
        nonlocal closed
        try:
            yield messages()[0]
            yield AssistantMessage([ToolUseBlock('id', 'Bash', {'command': 'bad'})], MODEL)
        finally:
            closed = True
    with pytest.raises(ValueError, match='tool-attempt'):
        await fg.consume_query('synthetic', options(), [], query)
    assert closed


@pytest.mark.asyncio
async def test_timeout_cleans_up_without_retry(monkeypatch):
    import asyncio
    closed = False
    async def pending(*args):
        nonlocal closed
        try:
            await asyncio.Event().wait()
        finally:
            closed = True
    monkeypatch.setattr(fg, 'consume_query', pending)
    with pytest.raises(TimeoutError):
        await fg.bounded_query({'prompt': '', 'vocabulary': [], 'timeout': .01}, options())
    assert closed


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_cleans_up(monkeypatch):
    import asyncio
    ready = asyncio.Event()
    closed = False
    async def pending(*args):
        nonlocal closed
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed = True
    monkeypatch.setattr(fg, 'consume_query', pending)
    task = asyncio.create_task(fg.bounded_query({'prompt': '', 'vocabulary': [], 'timeout': 1}, options()))
    await ready.wait()
    from server.aio import cancel_and_reap
    await cancel_and_reap(task, phase='test-framework-cancel')
    assert task.cancelled() and closed


def test_command_options_no_tools_and_bare():
    configured = options()
    assert configured.tools == []
    assert configured.mcp_servers == {}
    assert configured.strict_mcp_config is True
    assert configured.setting_sources == []
    assert configured.extra_args == {'safe-mode': None, 'disable-slash-commands': None, 'system-prompt-snapshot': 'off', 'no-session-persistence': None}
    assert configured.max_turns == 1
    assert configured.max_budget_usd == .1
    assert configured.system_prompt == fg.SYSTEM_PROMPT
    assert configured.env == {'CLAUDE_CONFIG_DIR': '/scratch/config'}


def test_capability_preflight_rejects_missing_bare(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(fg.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='--tools'))
    with pytest.raises(ValueError, match='unsupported-native-capability'):
        fg.preflight('/engine', '/scratch', {})


def test_paired_order_and_budget_reservations(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    prompts = []
    def call(request, env):
        prompts.append(request)
        return {'status': 'completed', 'executed': True, 'cost_usd': .01, 'usage': None,
                'observed_model': MODEL, 'tools_observed': [], 'findings': [],
                'auth_mode': 'claude.ai', 'subscription_type': 'max'}
    monkeypatch.setattr(fg, 'worker_call', call)
    result = fg.run_comparison(ROOT, model=MODEL, budget=1.2, timeout=30)
    assert result['status'] == 'completed'
    assert [c['arm'] for c in result['calls']] == ['baseline', 'guidance', 'guidance', 'baseline', 'baseline', 'guidance']
    assert sum(r['budget'] for r in prompts) <= 1.2
    assert result['totals']['baseline']['false_negatives'] == 2
    assert result['totals']['guidance']['false_negatives'] == 2


@pytest.mark.parametrize('outcome', [{'status': 'completed', 'executed': False, 'cost_usd': .01},
                                   {'status': 'completed', 'executed': True, 'cost_usd': None},
                                   {'status': 'worker-failed', 'executed': True}])
def test_unknown_failure_stops_remaining_calls(monkeypatch, outcome):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    calls = []
    monkeypatch.setattr(fg, 'worker_call', lambda request, env: calls.append(request) or outcome)
    result = fg.run_comparison(ROOT, model=MODEL, budget=1, timeout=30)
    assert result['status'] == 'incomplete'
    assert len(calls) == 1
    assert result['reserved_usd'] == pytest.approx(1 / 6)


def test_mismatched_guidance_rejected(monkeypatch):
    original = Path.read_bytes
    def tamper(path):
        data = original(path)
        return data + b' altered' if str(path).endswith('/reference/operate.md') else data
    monkeypatch.setattr(Path, 'read_bytes', tamper)
    with pytest.raises(ValueError):
        fg.load_inputs(ROOT)


def test_empty_corpus_rejected(monkeypatch):
    original = Path.read_bytes
    def tamper(path):
        return b'fixtures: []' if path.name == fg.CASE_ID + '.yaml' else original(path)
    monkeypatch.setattr(Path, 'read_bytes', tamper)
    with pytest.raises(ValueError, match='invalid-fixture-selection'):
        fg.load_inputs(ROOT)


def test_run_evals_explicit_mode_and_no_key(monkeypatch, tmp_path, capsys):
    from scripts import run_evals
    from server.sessions import helper_call
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: None)
    assert run_evals.main(['--kind', 'agent']) == 0
    assert 'LIST ONLY' in capsys.readouterr().out
    for arguments in (['--kind', 'agent', '--execute'],
                      ['--kind', 'agent', '--execute', '--case', 'example-agent-quick-fix'],
                      ['--execute'], ['--kind', 'agent', '--case', 'missing']):
        with pytest.raises(SystemExit):
            run_evals.main(arguments)
    path = tmp_path / 'private.json'
    assert run_evals.main(['--kind', 'agent', '--execute', '--case', fg.CASE_ID, '--model', MODEL,
                           '--max-budget-usd', '1', '--report', str(path)]) == 1
    assert json.loads(path.read_text())['executed'] is False


def test_sdk_command_builds_empty_tools_and_bare():
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    command = SubprocessCLITransport(prompt='synthetic', options=options())._build_command()
    assert command[command.index('--tools') + 1] == ''
    assert '--setting-sources=' in command
    for flag in ('--safe-mode', '--disable-slash-commands', '--no-session-persistence', '--strict-mcp-config', '--max-budget-usd'):
        assert flag in command


def test_worker_missing_config_does_not_resolve_or_query(monkeypatch):
    from server.sessions import claude_bin
    from server.sessions import helper_call
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: None)
    monkeypatch.setattr(claude_bin, 'resolve_claude_bin', lambda: pytest.fail('resolved binary'))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    assert fg.worker_main({'model': MODEL, 'budget': .1, 'timeout': 30}) == {
        'status': 'missing-subscription-account', 'executed': False}


@pytest.mark.asyncio
async def test_model_mismatch_rejected():
    values = messages()
    values[0].data['model'] = 'different-model'
    async def query(**kwargs):
        for value in values:
            yield value
    with pytest.raises(ValueError, match='unexpected-init-capabilities-or-model'):
        await fg.consume_query('synthetic', options(), [], query)


def test_worker_launch_environment_and_stdin(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'dedicated-secret')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'ambient-secret')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/ambient/account')
    captured = {}
    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            captured.update(command=command, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def communicate(self, text, **kwargs):
            captured['request'] = text
            return ('{"status":"missing-api-key","executed":false}', None)
    monkeypatch.setattr(fg.subprocess, 'Popen', Process)
    result = fg.worker_call({'prompt': 'synthetic', 'timeout': 30}, fg.worker_environment(None))
    assert result['executed'] is False
    assert '-I' in captured['command']
    assert 'ANTHROPIC_AUTH_TOKEN' not in captured['env']
    assert captured['env']['CLAUDE_CONFIG_DIR'] == '/fake/scratch'
    assert 'ANTHROPIC_API_KEY' not in captured['env']
    assert 'secret' not in captured['request']
    assert 'secret' not in ' '.join(captured['command'])


def test_report_does_not_follow_symlink(tmp_path):
    original = tmp_path / 'original'
    original.write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(original)
    with pytest.raises(FileExistsError):
        fg.reserve_report(link)
    assert original.read_text() == 'keep'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['tool', 'bad-result', 'cancel'])
async def test_nested_sdk_generator_cleanup_before_return(failure):
    import asyncio
    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    from server.aio import cancel_and_reap
    waiting = asyncio.Event()
    closed = asyncio.Event()
    async def inner():
        try:
            yield messages()[0]
            if failure == 'tool':
                yield AssistantMessage([ToolUseBlock('id', 'Bash', {})], MODEL)
            elif failure == 'bad-result':
                yield messages(error=True)[1]
            waiting.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()
    async def wrapper(**kwargs):
        # Same closure non-forwarding shape as the public SDK query().
        async for message in inner():
            yield message
    task = asyncio.create_task(fg.consume_query('synthetic', options(), [], wrapper))
    await waiting.wait()
    if failure == 'cancel':
        await cancel_and_reap(task, phase='test-guidance-nested-cancellation')
        assert task.cancelled()
    else:
        with pytest.raises(ValueError):
            await task
    assert closed.is_set(), 'native cleanup must precede return/cwd deletion'


@pytest.mark.parametrize('destination', ['existing', 'dangling-link', 'non-directory', 'permission'])
def test_cli_reserves_report_before_comparison(monkeypatch, tmp_path, capsys, destination):
    from scripts import run_evals
    path = tmp_path / 'report.json'
    if destination == 'existing':
        path.write_text('keep')
    elif destination == 'dangling-link':
        path.symlink_to(tmp_path / 'missing')
    elif destination == 'non-directory':
        parent = tmp_path / 'file'
        parent.write_text('keep')
        path = parent / 'report.json'
    else:
        def denied(*args, **kwargs):
            raise PermissionError('private destination detail')
        monkeypatch.setattr(fg.os, 'open', denied)
    calls = []
    monkeypatch.setattr(fg, 'run_comparison', lambda *args, **kwargs: calls.append(1) or
                        {'status': 'completed', 'executed': True})
    with pytest.raises(SystemExit):
        run_evals.main(['--kind', 'agent', '--execute', '--case', fg.CASE_ID,
                       '--model', MODEL, '--max-budget-usd', '1', '--report', str(path)])
    assert calls == []
    assert 'private destination detail' not in capsys.readouterr().err
    if destination == 'existing':
        assert path.read_text() == 'keep'
    if destination == 'dangling-link':
        assert path.is_symlink() and not path.exists()


def test_cli_persists_marker_before_comparison(monkeypatch, tmp_path):
    from scripts import run_evals
    path = tmp_path / 'report.json'
    def comparison(*args, **kwargs):
        marker = json.loads(path.read_text())
        assert marker['status'] == 'incomplete'
        assert marker['executed'] is False
        assert path.stat().st_mode & 0o777 == 0o600
        raise RuntimeError('interrupted comparison')
    monkeypatch.setattr(fg, 'run_comparison', comparison)
    with pytest.raises(RuntimeError, match='interrupted comparison'):
        run_evals.main(['--kind', 'agent', '--execute', '--case', fg.CASE_ID,
                       '--model', MODEL, '--max-budget-usd', '1', '--report', str(path)])
    assert json.loads(path.read_text())['status'] == 'incomplete'


def test_cli_final_report_uses_reserved_descriptor(monkeypatch, tmp_path):
    from scripts import run_evals
    path = tmp_path / 'report.json'
    reserved = tmp_path / 'reserved.json'
    target = tmp_path / 'untouched.json'
    target.write_text('keep')
    def comparison(*args, **kwargs):
        path.rename(reserved)
        path.symlink_to(target)
        return {'status': 'completed', 'executed': True}
    monkeypatch.setattr(fg, 'run_comparison', comparison)
    assert run_evals.main(['--kind', 'agent', '--execute', '--case', fg.CASE_ID,
                          '--model', MODEL, '--max-budget-usd', '1', '--report', str(path)]) == 0
    assert json.loads(reserved.read_text()) == {'status': 'completed', 'executed': True}
    assert reserved.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == 'keep'


@pytest.mark.parametrize('failure,reason', [
    *[(ValueError(reason), reason) for reason in (
        'unexpected-init-capabilities-or-model', 'invalid-assistant-message',
        'tool-attempt', 'missing-or-failed-result', 'unknown-or-exhausted-cost',
        'missing-executed-result', 'invalid-response-schema', 'invalid-finding-ids',
        'unsupported-native-capability', 'unverified-native-version',
        'native-engine-unavailable',
    )],
    (TimeoutError('query-timeout'), 'query-timeout'),
    (json.JSONDecodeError('secret-shaped detail', 'sk-ant-private-value', 0), 'invalid-response-json'),
    (ValueError('sk-ant-private-value'), 'native-error'),
    (RuntimeError('sk-ant-private-value'), 'native-error'),
    (RuntimeError('tool-attempt'), 'native-error'),
    (ValueError('tool-attempt', 'sk-ant-private-value'), 'native-error'),
    (KeyboardInterrupt('sk-ant-private-value'), 'native-error'),
])
def test_worker_failure_reason_is_bounded_and_redacted(monkeypatch, failure, reason):
    from server.sessions import claude_bin
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/original/test-config')
    monkeypatch.setattr(claude_bin, 'resolve_claude_bin', lambda: '/verified/claude')
    monkeypatch.setattr(fg, 'preflight', lambda *args: '2.1.293')
    monkeypatch.setattr(fg, 'subscription_auth', lambda *args: {'auth_mode': 'claude.ai', 'subscription_type': 'max'})
    async def fail(*args):
        raise failure
    monkeypatch.setattr(fg, 'bounded_query', fail)
    report = fg.worker_main({'model': MODEL, 'budget': .1, 'timeout': 30})
    assert report['status'] == 'worker-failed'
    assert report['failure_reason'] == reason
    assert report['executed'] is True
    assert report['cost_usd'] is None and report['usage'] is None
    assert 'sk-ant-private-value' not in json.dumps(report)
    assert 'secret-shaped detail' not in json.dumps(report)


@pytest.mark.parametrize('provider,ready', [('openai', True), ('copilot', True), (None, True), ('anthropic', False)])
def test_explicit_account_rejected_without_fallback(monkeypatch, provider, ready):
    from server.sessions import helper_call
    from viberpit import accounts
    monkeypatch.setattr(accounts, 'provider_or_none', lambda name: provider)
    monkeypatch.setattr(helper_call, 'is_account_helper_ready', lambda name: ready)
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: pytest.fail('fallback'))
    with pytest.raises(ValueError, match='missing-subscription-account'):
        fg.worker_environment('chosen')


def test_explicit_account_passed_to_canonical_helper(monkeypatch):
    from server.sessions import account_auth, helper_call
    observed = []
    monkeypatch.setattr(helper_call, 'pick_helper_config_dir', lambda: pytest.fail('fallback'))
    def helper(config, account):
        observed.append((config, account))
        return {'CLAUDE_CONFIG_DIR': '/scratch/chosen'}
    monkeypatch.setattr(account_auth, 'helper_env', helper)
    assert fg.worker_environment('chosen')['CLAUDE_CONFIG_DIR'] == '/scratch/chosen'
    assert observed == [('/fake/chosen', 'chosen')]


def test_ambient_oauth_cannot_replace_missing_canonical_token(monkeypatch):
    import os
    from server.sessions import account_auth
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'ambient-secret')
    monkeypatch.setattr(account_auth, 'oauth_env_for', lambda account: {})
    monkeypatch.setattr(account_auth, 'helper_env', lambda *a, **k: {**os.environ, 'CLAUDE_CONFIG_DIR': '/scratch'})
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in fg.worker_environment(None)
    assert os.environ['CLAUDE_CODE_OAUTH_TOKEN'] == 'ambient-secret'


@pytest.mark.parametrize('changes', [
    {'loggedIn': False}, {'authMethod': 'api_key'}, {'apiProvider': 'bedrock'},
    {'subscriptionType': None}, {'subscriptionType': 'unknown'}, {'loggedIn': 1},
])
def test_auth_probe_rejects_non_subscription(monkeypatch, changes):
    from types import SimpleNamespace
    value = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}
    value.update(changes)
    monkeypatch.setattr(fg.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(value)))
    with pytest.raises(ValueError, match='unverified-subscription-auth'):
        fg.subscription_auth('/native', '/work', {})


@pytest.mark.parametrize('tier', ['max', 'pro'])
def test_auth_probe_returns_only_allowlisted_metadata(monkeypatch, tier):
    from types import SimpleNamespace
    def run(command, **kwargs):
        assert command == ['/native', 'auth', 'status', '--json']
        return SimpleNamespace(stdout=json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
            'apiProvider': 'firstParty', 'subscriptionType': tier, 'email': 'private@example.org'}))
    monkeypatch.setattr(fg.subprocess, 'run', run)
    assert fg.subscription_auth('/native', '/work', {}) == {'auth_mode': 'claude.ai', 'subscription_type': tier}


def test_stale_subscription_stops_before_inference(monkeypatch):
    from server.sessions import claude_bin
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/scratch')
    monkeypatch.setattr(claude_bin, 'resolve_claude_bin', lambda: '/native')
    monkeypatch.setattr(fg, 'preflight', lambda *a: '2.1.293')
    def reject(*a):
        raise ValueError('unverified-subscription-auth')
    monkeypatch.setattr(fg, 'subscription_auth', reject)
    monkeypatch.setattr(fg, 'bounded_query', lambda *a: pytest.fail('inference'))
    report = fg.worker_main({'model': MODEL, 'budget': .1, 'timeout': 30})
    assert report['executed'] is False
    assert report['failure_reason'] == 'unverified-subscription-auth'


def test_account_environment_frozen_for_all_calls(monkeypatch):
    selections = []
    environment = {'CLAUDE_CONFIG_DIR': '/scratch', 'CLAUDE_CODE_OAUTH_TOKEN': 'private'}
    monkeypatch.setattr(fg, 'worker_environment', lambda account: selections.append(account) or environment)
    environments = []
    def worker(request, env):
        environments.append(env)
        return {'status': 'completed', 'executed': True, 'cost_usd': 0, 'usage': None,
                'observed_model': MODEL, 'tools_observed': [], 'findings': [],
                'auth_mode': 'claude.ai', 'subscription_type': 'max'}
    monkeypatch.setattr(fg, 'worker_call', worker)
    result = fg.run_comparison(ROOT, model=MODEL, budget=1, timeout=30, account='chosen')
    assert result['status'] == 'completed'
    assert selections == ['chosen'] and len(environments) == 6
    assert all(env is environment for env in environments)
    assert 'private' not in json.dumps(result) and '/scratch' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['tools', 'mcp_servers', 'slash_commands', 'skills'])
async def test_inherited_capabilities_rejected(key):
    values = messages()
    values[0].data[key] = ['unexpected']
    async def query(**kwargs):
        for value in values:
            yield value
    with pytest.raises(ValueError, match='unexpected-init-capabilities-or-model'):
        await fg.consume_query('synthetic', options(), [], query)


@pytest.mark.asyncio
async def test_documented_builtin_plugins_do_not_imply_installed_skills():
    values = messages(cost=0)
    values[0].data['plugins'] = ['cc-plugin-agents-md', 'cc-plugin-telemetry', 'cc-plugin-plugin-authoring']
    async def query(**kwargs):
        for value in values:
            yield value
    result = await fg.consume_query('synthetic', options(), [], query)
    assert result['cost_usd'] == 0


@pytest.mark.parametrize('auth', [{}, {'auth_mode': 'api_key', 'subscription_type': 'max'},
                                  {'auth_mode': 'claude.ai', 'subscription_type': 'unknown'}])
def test_success_without_verified_subscription_stops_comparison(monkeypatch, auth):
    calls = []
    def worker(request, env):
        calls.append(1)
        return {'status': 'completed', 'executed': True, 'cost_usd': 0,
                'observed_model': MODEL, 'tools_observed': [], 'findings': [], **auth}
    monkeypatch.setattr(fg, 'worker_call', worker)
    assert fg.run_comparison(ROOT, model=MODEL, budget=1, timeout=30)['status'] == 'incomplete'
    assert calls == [1]


@pytest.mark.asyncio
async def test_authentication_error_is_bounded():
    from claude_agent_sdk import AssistantMessage
    async def query(**kwargs):
        yield messages()[0]
        yield AssistantMessage([], MODEL, error='authentication_failed')
    with pytest.raises(ValueError, match='authentication-failed'):
        await fg.consume_query('synthetic', options(), [], query)


def test_expired_envelope_never_falls_back_to_scratch_credentials(monkeypatch):
    from server.sessions import account_auth
    from viberpit import accounts
    monkeypatch.setattr(accounts, 'runner_uses_envelope', lambda name: True)
    monkeypatch.setattr(account_auth, 'oauth_env_for', lambda account: {})
    with pytest.raises(ValueError, match='missing-subscription-account'):
        fg.worker_environment(None)
