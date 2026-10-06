import json
import stat

import pytest

from server.codex.turn_history import TurnHistoryStore, decode_event
from server.sessions import events as ev


def turn(turn_id='turn1', text='answer'):
    return [ev.UserMessage(turn_id, 'question', images=[{'url': '/image', 'mime': 'image/png'}],
                           origin={'kind': 'automation', 'label': 'test'}, ts=1),
            ev.TurnStart(turn_id), ev.TurnModel(turn_id, 'model'),
            ev.SessionMeta('model', 'session'),
            ev.ThinkingDelta(turn_id, 'thinking', ts=2),
            ev.ToolCall(turn_id, 'tool', 'Bash', ev.ToolKind.BASH, {'command': 'pwd'}, ts=3),
            ev.ToolResult('tool', True, {'output': 'result'}, ts=4),
            ev.TextDelta(turn_id, text, ts=5),
            ev.ErrorEvent('code', 'message', turn_id=turn_id, ts=6),
            ev.TurnEnd(turn_id, outcome='hand_back', hand_back_reason='review')]


def test_round_trip_and_private_permissions(tmp_path):
    root = tmp_path / 'history'
    store = TurnHistoryStore(root)
    events = turn()
    store.record('thread1', 'turn1', events)
    loaded = TurnHistoryStore(root).load('thread1')
    assert [r.turn_id for r in loaded] == ['turn1']
    assert [e.to_wire() for e in loaded[0].events] == [e.to_wire() for e in events]
    assert isinstance(loaded[0].events[5].kind, ev.ToolKind)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / 'thread1.turns.jsonl').stat().st_mode) == 0o600
    assert store.load('other-thread') == []


def test_corruption_torn_tail_and_duplicate_turn(tmp_path):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', turn())
    path = tmp_path / 'thread.turns.jsonl'
    with path.open('a') as f:
        f.write('{broken}\n')
    store.record('thread', 'turn2', turn('turn2'))
    store.record('thread', 'turn1', turn(text='new answer'))
    with path.open('a') as f:
        f.write('{"events":')
    loaded = store.load('thread')
    assert [r.turn_id for r in loaded] == ['turn1', 'turn2']
    assert loaded[0].events[7].text == 'new answer'


@pytest.mark.parametrize('events', [[], [ev.TurnEnd('turn1')],
    [ev.TurnStart('wrong'), ev.TurnEnd('turn1')],
    [ev.TurnStart('turn1'), ev.TurnEnd('wrong')],
    [ev.TurnStart('turn1', parent_tool_use_id='child'), ev.TurnEnd('turn1')],
    [ev.TurnStart('turn1'), ev.TextDelta('wrong', 'text'), ev.TurnEnd('turn1')]])
def test_reject_partial_or_mismatched_turn(tmp_path, events):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', events)
    assert store.load('thread') == []


@pytest.mark.parametrize('thread_id', ['../escape', '/escape', '.', 'thread\n'])
def test_no_path_traversal(tmp_path, thread_id):
    store = TurnHistoryStore(tmp_path)
    store.record(thread_id, 'turn1', turn())
    assert store.load(thread_id) == []
    assert list(tmp_path.iterdir()) == []


def test_reject_malformed_whole_turn_and_cross_thread_record(tmp_path):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', turn())
    path = tmp_path / 'thread.turns.jsonl'
    record = json.loads(path.read_text())
    record['events'][7]['text'] = ['not text']
    path.write_text(json.dumps(record) + '\n')
    assert store.load('thread') == []
    store.record('thread', 'turn1', turn())
    (tmp_path / 'other.turns.jsonl').write_bytes(path.read_bytes())
    assert store.load('other') == []


def test_decoder_rejects_unknown_type_and_wrong_values():
    for wire in [{'type': 'os.system'}, {'type': 'textDelta', 'turnId': 't', 'text': []},
                 {'type': 'toolResult', 'toolId': 't', 'ok': 'yes', 'payload': {}},
                 {'type': 'textDelta', 'turnId': 't'}]:
        with pytest.raises(ValueError):
            decode_event(wire)


def test_io_failure_logs_no_content(tmp_path, caplog):
    root = tmp_path / 'file'
    root.write_text('not a directory')
    store = TurnHistoryStore(root)
    store.record('thread', 'turn1', turn(text='secret-content'))
    assert store.load('thread') == []
    assert 'turn_history' in caplog.text
    assert 'secret-content' not in caplog.text


def test_temporary_user_id_and_transport_envelope(tmp_path):
    store = TurnHistoryStore(tmp_path)
    events = turn()
    events[0].turn_id = 'temporary-prompt-uuid'
    store.record('thread', 'turn1', events)
    assert store.load('thread')[0].events[0].turn_id == 'temporary-prompt-uuid'
    decoded = decode_event({'type': 'textDelta', 'turnId': 'turn1', 'text': 'hello',
                            'sessionId': 'session', 'seq': 10})
    assert decoded.ts is None


def test_valid_append_after_torn_tail_and_invalid_duplicate(tmp_path):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', turn())
    path = tmp_path / 'thread.turns.jsonl'
    with path.open('a') as handle:
        handle.write('{"broken":')
    store.record('thread', 'turn2', turn('turn2'))
    store.record('thread', 'turn1', [ev.TurnStart('turn1')])
    assert [r.turn_id for r in store.load('thread')] == ['turn1', 'turn2']


def test_symlink_cannot_redirect_reads_or_writes(tmp_path):
    root = tmp_path / 'history'
    root.mkdir()
    other = tmp_path / 'elsewhere'
    other.write_text('private unrelated content')
    (root / 'thread.turns.jsonl').symlink_to(other)
    store = TurnHistoryStore(root)
    store.record('thread', 'turn1', turn())
    assert store.load('thread') == []
    assert other.read_text() == 'private unrelated content'


def test_oversized_numeric_timestamp_rejects_record_without_hiding_other_turns(tmp_path):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', turn())
    path = tmp_path / 'thread.turns.jsonl'
    record = json.loads(path.read_text())
    record['events'][7]['ts'] = 10**400
    with path.open('a') as handle:
        handle.write(json.dumps(record) + '\n')
    store.record('thread', 'turn2', turn('turn2'))
    assert [r.turn_id for r in store.load('thread')] == ['turn1', 'turn2']
    with pytest.raises(ValueError):
        decode_event(record['events'][7])


def test_delete_removes_only_selected_thread_and_is_idempotent(tmp_path):
    store = TurnHistoryStore(tmp_path)
    store.record('thread', 'turn1', turn())
    store.record('other', 'turn2', turn('turn2'))
    store.delete('thread')
    store.delete('thread')
    store.delete('../other')
    assert store.load('thread') == []
    assert [r.turn_id for r in store.load('other')] == ['turn2']
    assert not (tmp_path / 'thread.turns.jsonl').exists()


def test_delete_io_failure_is_best_effort(tmp_path, caplog):
    (tmp_path / 'thread.turns.jsonl').mkdir()
    TurnHistoryStore(tmp_path).delete('thread')
    assert 'codex.turn_history: history not deleted' in caplog.text
