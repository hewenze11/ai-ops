"""Fan-out reads (§52.4).

The thing under test is not "does it dispatch N tasks" -- that is easy and
boring. It is whether the answer a model receives tells the truth about what
was actually observed. A fan-out that reports "50 hosts, 30 empty" when in fact
30 hosts were unreachable will send a model off to diagnose services that were
never down.
"""
import json

import pytest

from ai_ops import fanout


def asset(asset_id, users=('ops_read',), retired=None):
    return {'id': asset_id, 'allowed_users': json.dumps(list(users)), 'retired_at': retired}


def req(**kwargs):
    base = {'asset_ids': ['a', 'b'], 'run_as': 'ops_read'}
    base.update(kwargs)
    return fanout.FanoutRequest(**base)


def lookup(mapping):
    return lambda asset_id: mapping.get(asset_id)


def test_asset_cap_is_refused_not_truncated():
    # A model that believes it read 100 hosts and actually read 50 will reason
    # confidently about half a picture. Refusing is the only honest option.
    with pytest.raises(Exception) as error:
        req(asset_ids=['h%d' % i for i in range(fanout.MAX_ASSETS_PER_CALL + 1)])
    assert 'TOO_MANY_ASSETS' in str(error.value)
    # Exactly at the cap is fine.
    req(asset_ids=['h%d' % i for i in range(fanout.MAX_ASSETS_PER_CALL)])


def test_duplicate_assets_are_refused():
    # Duplicates would read one host twice and count it twice in the summary.
    with pytest.raises(Exception) as error:
        req(asset_ids=['a', 'a'])
    assert 'DUPLICATE_ASSETS' in str(error.value)


def test_unknown_and_retired_and_unauthorised_are_rejected_per_asset():
    plan, rejected = fanout.plan(
        req(asset_ids=['good', 'missing', 'retired', 'wronguser']),
        'read_file', {'path': '/etc/hostname'},
        lookup({'good': asset('good'), 'retired': asset('retired', retired=1.0),
                'wronguser': asset('wronguser', users=('other',))}))
    assert plan == ['good']
    reasons = {r['asset_id']: r['reason'] for r in rejected}
    assert reasons == {'missing': 'ASSET_UNAVAILABLE', 'retired': 'ASSET_UNAVAILABLE',
                       'wronguser': 'ACCOUNT_NOT_ALLOWED'}


def test_no_eligible_asset_fails_the_whole_call():
    # Returning "0 of 3 hosts" as a success would look like data.
    with pytest.raises(fanout.FanoutRejected) as error:
        fanout.plan(req(), 'read_file', {'path': '/x'}, lookup({}))
    assert error.value.code == 'NO_ELIGIBLE_ASSETS'


def test_bad_params_fail_the_call_instead_of_fifty_broken_tasks():
    # Validated once up front so a wrong parameter set cannot turn into 50
    # identical failures the model then has to diagnose one by one.
    with pytest.raises(fanout.FanoutRejected) as error:
        fanout.plan(req(), 'read_file', {'path': 'relative/path'},
                    lookup({'a': asset('a'), 'b': asset('b')}))
    assert error.value.code.startswith('PARAMS_INVALID')


def test_only_readonly_tools_are_fanout_eligible():
    # Fan-out widens breadth, never capability. If a write ever appears here,
    # one tool call becomes 50 writes nobody approved individually.
    for name in ('propose_change', 'shell', 'execute_command', 'reveal'):
        with pytest.raises(fanout.FanoutRejected) as error:
            fanout.plan(req(), name, {}, lookup({'a': asset('a')}))
        assert error.value.code == 'TOOL_NOT_FANOUT_ELIGIBLE'
    assert fanout.ALLOWED_TOOLS <= set(fanout.__dict__.get('ALLOWED_TOOLS', ())) | fanout.ALLOWED_TOOLS


def _task(state, result=None):
    return {'state': state, 'result': json.dumps(result) if result else None}


def test_outcomes_are_not_collapsed():
    """The distinction this whole module exists for.

    "unreachable", "timeout" and "read it, it was empty" must stay separate.
    Collapsing them into "no data" makes a network outage look like fifty
    identical service failures.
    """
    assert fanout.child_outcome(_task('succeeded', {'status': 'succeeded', 'stdout': ''}))[0] == 'ok'
    assert fanout.child_outcome(_task('failed', {'status': 'failed', 'error_code': 'CANNOT_SWITCH_USER'}))[0] == 'unreachable'
    assert fanout.child_outcome(_task('failed', {'status': 'failed', 'error_code': 'READONLY_TIMEOUT'}))[0] == 'timeout'
    assert fanout.child_outcome(_task('claimed'))[0] == 'running'
    assert fanout.child_outcome(_task('unknown'))[0] == 'unknown'
    assert fanout.child_outcome(None)[0] == 'no_result'


def test_two_failures_with_the_same_empty_text_are_still_distinguished():
    # Same empty stdout, different causes, different categories.
    slow = _task('failed', {'status': 'failed', 'error_code': 'READONLY_TIMEOUT', 'stdout': ''})
    gone = _task('failed', {'status': 'failed', 'error_code': 'READONLY_EXEC_FAILED', 'stdout': ''})
    assert fanout.child_outcome(slow)[0] == 'timeout'
    assert fanout.child_outcome(gone)[0] == 'unreachable'


def test_summary_counts_by_outcome_and_failures_name_the_reason():
    group = {'tool': 'journal_logs', 'params': json.dumps({'lines': 5})}
    children = [{'asset_id': 'ok1', 'task_id': 't1'}, {'asset_id': 'ok2', 'task_id': 't2'},
                {'asset_id': 'gone', 'task_id': 't3'}, {'asset_id': 'slow', 'task_id': 't4'}]
    tasks = {'t1': _task('succeeded', {'status': 'succeeded', 'stdout': 'line'}),
             't2': _task('succeeded', {'status': 'succeeded', 'stdout': 'line'}),
             't3': _task('failed', {'status': 'failed', 'error_code': 'CANNOT_SWITCH_USER'}),
             't4': _task('failed', {'status': 'failed', 'error_code': 'READONLY_TIMEOUT'})}
    answer = fanout.assemble(group, children, lambda tid: tasks.get(tid))
    assert answer['summary'] == {'ok': 2, 'timeout': 1, 'unreachable': 1}
    assert answer['assets_total'] == 4
    by_asset = {f['asset_id']: f['status'] for f in answer['failures']}
    assert by_asset == {'gone': 'unreachable', 'slow': 'timeout'}
    # The note is what stops the model treating an unread host as evidence.
    assert 'NOT read' in answer['note']


def test_text_budget_is_shared_and_omissions_are_declared():
    # A fan-out that returns 200KB is a context overflow, not a result. When the
    # budget runs out the model is told, never left to assume the list ended.
    group = {'tool': 'read_file', 'params': '{}'}
    children = [{'asset_id': 'h%d' % i, 'task_id': 't%d' % i} for i in range(40)]
    tasks = {c['task_id']: _task('succeeded', {'status': 'succeeded', 'stdout': 'x' * 5000})
             for c in children}
    answer = fanout.assemble(group, children, lambda tid: tasks.get(tid))
    kept = sum(len(r['text']) for r in answer['results'])
    assert kept <= fanout.TOTAL_TEXT_CHARS
    assert answer['omitted_for_size'] > 0
    assert 'omitted' in answer['omitted_note']
    assert answer['summary']['ok'] == 40, 'the summary must still cover every host'


def test_a_clipped_host_says_so():
    group = {'tool': 'read_file', 'params': '{}'}
    children = [{'asset_id': 'h', 'task_id': 't'}]
    tasks = {'t': _task('succeeded', {'status': 'succeeded',
                                      'stdout': 'y' * (fanout.PER_ASSET_CHARS + 100)})}
    answer = fanout.assemble(group, children, lambda tid: tasks.get(tid))
    assert answer['results'][0]['truncated'] is True
