"""Goal work/evaluator acceptance through independent production applications."""
import json
import sqlite3

import pytest

from tests.test_sdk.test_mixed_writer_process import run_mixed_process_acceptance
from voidx.platform.session_ids import validate_session_storage_id


def assert_goal_identity(workspace, directory, legacy_events, sdk_events):
    with sqlite3.connect(workspace / '.voidx' / 'store' / 'voidx.db') as db:
        db.row_factory = sqlite3.Row
        bindings = [dict(r) for r in db.execute('SELECT * FROM goal_generations')]
        records = [dict(r) for r in db.execute('SELECT * FROM goal_protocol_records ORDER BY sequence_number')]
        threads = dict(db.execute('SELECT id, session_id FROM agent_threads'))
        transcripts = [dict(r) for r in db.execute('SELECT * FROM goal_transcript_records')]
    assert len(bindings) == 1, directory
    binding = bindings[0]
    main, work, evaluator = (binding[k] for k in ('main_session_id', 'work_session_id', 'evaluator_session_id'))
    assert len({main, work, evaluator}) == 3
    for sid in (main, work, evaluator):
        assert validate_session_storage_id(sid) == sid
    assert threads[binding['goal_thread_id']] == work
    assert any(r['session_id'] == evaluator and r['phase'] == 'decision' for r in records)
    assert main == next(e['parent_id'] for e in legacy_events if e['event'] == 'boot')
    expected = {'init': main, 'checkpoint': work, 'decision': evaluator}
    assert {'init', 'checkpoint', 'decision'} <= {r['phase'] for r in records}
    for record in records:
        assert record['session_id'] == expected[record['phase']]
        assert record['parent_session_id'] == main
        assert record['generation'] == binding['generation']
        assert record['status'] == 'projected'
    assert {work, evaluator} <= {r['session_id'] for r in transcripts}
    histories = json.loads((directory / 'histories.json').read_text())
    def calls(messages):
        return {c['name'] for m in messages for c in (m.get('tool_calls') or m.get('calls') or [])}
    assert 'goal_checkpoint' in calls(histories[work])
    assert 'goal_decision' in calls(histories[evaluator])
    assert 'goal_decision' not in calls(histories[work])
    assert not {'bash', 'goal_checkpoint'} & calls(histories[evaluator])
    assert not {'goal_checkpoint', 'goal_decision', 'bash'} & calls(histories[main])
    phases = [e for e in legacy_events if e['event'] == 'goal_history']
    assert {'work', 'evaluator'} == {e['phase'] for e in phases}
    for event in phases:
        assert 'SDK_PROCESS_WRITE' not in json.dumps(event['messages'])
        if event['phase'] == 'evaluator':
            assert not {'bash', 'goal_checkpoint'} & calls(event['messages'])
        else:
            assert 'goal_decision' not in calls(event['messages'])
    sdk_histories = [e for e in sdk_events if e['event'] == 'history']
    assert sdk_histories
    assert all(not {'goal_checkpoint', 'goal_decision'} & calls(e['messages']) for e in sdk_histories)
    boots = [next(e for e in events if e['event'] == 'boot') for events in (legacy_events, sdk_events)]
    assert boots[0]['pid'] != boots[1]['pid']
    assert boots[0]['data_dir'] == boots[1]['data_dir'] == str(workspace / '.voidx')
    return dict(binding=binding, protocol_count=len(records), transcript_count=len(transcripts),
                model_phase_counts={p: sum(e['phase'] == p for e in phases) for p in ('work', 'evaluator')},
                history_message_counts={sid: len(messages) for sid, messages in histories.items()},
                history_isolated=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['legacy-sdk', 'sdk-legacy'])
async def test_goal_production_mixed_process_lock_and_evaluator_identity(tmp_path, direction):
    await run_mixed_process_acceptance(tmp_path, direction, goal=True)
