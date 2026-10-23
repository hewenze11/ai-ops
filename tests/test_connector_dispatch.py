"""Connector run() dispatch: every transport must respect `kind`.

Regression guard for a real gap: both connectors used to read
``payload['command']`` unconditionally, so a ``kind=readonly`` task that the
authority layer had already allowed would either crash on a KeyError or, worse,
fall through to a shell path. These tests pin the dispatch instead.
"""
import json

import pytest

from ai_ops import actions, connector_kinds
from ai_ops.connector_local import LocalExecutor
from ai_ops.connector_ssh import ConnectorExecutor


class _NoopConnection:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


import sqlite3


def _transaction_factory():
    """A real in-memory database: the executors write leases and results, so a
    fake connection would only be tested against itself."""
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE task_leases(task_id TEXT, closed_at REAL)')
    db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, state TEXT, result TEXT, updated_at REAL, turn_id TEXT)')
    db.execute('CREATE TABLE role_turns(id TEXT PRIMARY KEY, state TEXT, source TEXT)')
    db.execute('INSERT INTO role_turns(id, state, source) VALUES("t1", "waiting_tool", "chat")')
    db.execute('INSERT INTO tasks(id, state, turn_id) VALUES("t1", "claimed", "t1")')

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    return _Ctx()


def _ssh():
    return ConnectorExecutor(_transaction_factory, lambda *a, **k: None)


def _local():
    return LocalExecutor(_transaction_factory, lambda *a, **k: None)


def test_ssh_unknown_kind_fails_closed(monkeypatch):
    """A kind this build does not know must not be guessed at."""
    executor = _ssh()
    monkeypatch.setattr(executor, '_connection', lambda asset_id: None)
    result = executor.run('a1', {'id': 't1', 'payload': {'kind': 'teleport', 'run_as': 'ops'}}, None)
    assert result['status'] == 'failed'
    assert result['error_code'].startswith('KIND_UNKNOWN')


def test_ssh_readonly_never_reaches_the_shell(monkeypatch):
    """readonly must be built from tool+params, not from a command string."""
    executor = _ssh()
    monkeypatch.setattr(executor, '_connection', lambda asset_id: None)
    captured = {}

    def fake_execute(info, run_as, command, timeout, cancel_event, remote=None):
        captured.update({'command': command, 'run_as': run_as, 'remote': remote})
        return {'claim_id': None, 'status': 'succeeded', 'exit_code': 0,
                'stdout': 'ok', 'stderr': '', 'error_code': None,
                'output_truncated': False}

    monkeypatch.setattr(executor, '_execute', fake_execute)
    payload = {'kind': 'readonly', 'tool': 'read_file', 'asset_id': 'a1',
               'params': {'path': '/etc/hostname'}}
    result = executor.run('a1', {'id': 't1', 'payload': payload}, None)
    assert result['status'] == 'succeeded'
    assert captured['command'] is None, 'readonly must not carry a shell string'
    assert captured['run_as'] is None, 'a read must not elevate'
    assert captured['remote'].startswith('/usr/bin/cat ')
    assert '/bin/sh' not in captured['remote']


def test_ssh_readonly_rejects_tampered_params_before_connecting(monkeypatch):
    executor = _ssh()
    called = []
    monkeypatch.setattr(executor, '_connection',
                        lambda asset_id: called.append(asset_id) or None)
    payload = {'kind': 'readonly', 'tool': 'read_file', 'asset_id': 'a1',
               'params': {'path': '/etc/hostname'}, 'args': ['--', '/etc/shadow']}
    result = executor.run('a1', {'id': 't1', 'payload': payload}, None)
    assert result['status'] == 'failed'
    assert 'READONLY_REJECTED' in result['error_code']
    assert called == [], 'an invalid task must be refused before any connection'


def test_ssh_change_rejects_bad_hash_without_running(monkeypatch):
    executor = _ssh()
    monkeypatch.setattr(executor, '_connection', lambda asset_id: None)
    step = actions.expand('service.restart', {'unit': 'nginx.service'})
    payload = {'kind': 'change', 'asset_id': 'a1', 'run_as': 'ops',
               'steps': [step], 'approval': {'payload_sha256': 'f' * 64}}
    result = executor.run('a1', {'id': 't1', 'payload': payload}, None)
    assert 'APPROVAL_HASH_MISMATCH' in result['error_code']


def test_ssh_change_stops_after_first_failure(monkeypatch):
    """A change set is atomic in intent: report exactly what ran."""
    executor = _ssh()
    monkeypatch.setattr(executor, '_connection', lambda asset_id: None)
    first = actions.expand('service.restart', {'unit': 'a.service'})
    second = actions.expand('service.reload', {'unit': 'b.service'})
    digest = actions.payload_hash([first, second], 'a1', 'ops')
    payload = {'kind': 'change', 'asset_id': 'a1', 'run_as': 'ops',
               'steps': [first, second], 'approval': {'payload_sha256': digest}}
    ran = []

    def fake_execute(info, run_as, command, timeout, cancel_event, remote=None):
        ran.append(remote)
        return {'claim_id': None, 'status': 'failed', 'exit_code': 1,
                'stdout': '', 'stderr': 'nope', 'error_code': 'EXECUTION_FAILED',
                'output_truncated': False}

    monkeypatch.setattr(executor, '_execute', fake_execute)
    result = executor.run('a1', {'id': 't1', 'payload': payload}, None)
    assert len(ran) == 1, 'the second step must not run after the first fails'
    assert 'skipped after failure' in result['stdout']
    assert result['status'] == 'failed'


def test_local_rejects_change_kind():
    """The control host must not be a change target: it runs the control
    plane, so approving a write there is approving self-modification."""
    executor = _local()
    result = executor.run('local', {'id': 't1', 'payload': {'kind': 'change', 'steps': []}}, None)
    assert result['status'] == 'failed'
    assert 'KIND_NOT_SUPPORTED_ON_LOCAL' in result['error_code']


def test_local_unknown_kind_fails_closed():
    executor = _local()
    result = executor.run('local', {'id': 't1', 'payload': {'kind': 'teleport'}}, None)
    assert result['status'] == 'failed'
    assert 'KIND_NOT_SUPPORTED_ON_LOCAL' in result['error_code']


def test_local_readonly_builds_argv_not_a_command(monkeypatch):
    executor = _local()
    captured = {}

    def fake_argv(run_as, argv, timeout, cancel_event, base):
        captured.update({'run_as': run_as, 'argv': argv})
        base.update({'status': 'succeeded', 'exit_code': 0})
        return base

    monkeypatch.setattr(executor, '_execute_argv', fake_argv)
    payload = {'kind': 'readonly', 'tool': 'list_dir', 'params': {'path': '/var/log'}}
    result = executor.run('local', {'id': 't1', 'payload': payload}, None)
    assert result['status'] == 'succeeded'
    assert captured['argv'][:2] == ['/usr/bin/ls', '-a']


def test_local_readonly_tampered_argv_is_refused(monkeypatch):
    executor = _local()
    monkeypatch.setattr(executor, '_execute_argv',
                        lambda *a, **k: pytest.fail('must not execute'))
    payload = {'kind': 'readonly', 'tool': 'read_file',
               'params': {'path': '/etc/hostname'}, 'program': '/bin/sh'}
    result = executor.run('local', {'id': 't1', 'payload': payload}, None)
    assert 'READONLY_REJECTED' in result['error_code']
