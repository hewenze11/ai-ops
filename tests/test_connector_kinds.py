"""Connector-side kinds: readonly and change over SSH without an Agent.

The point of these tests is adversarial, not happy-path: a connector has only
ONE validator (the control plane), so every rejection here is the last line of
defence rather than a second one.
"""
import json

import pytest

from ai_ops import actions, connector_kinds, readonly_tools


# --- readonly ---------------------------------------------------------------

def test_readonly_derives_argv_from_tool_and_params():
    task = {'kind': 'readonly', 'tool': 'read_file', 'params': {'path': '/etc/hostname'}}
    program, argv = connector_kinds.readonly_command(task)
    assert program == '/usr/bin/cat'
    assert argv == ['--number', '--', '/etc/hostname']


def test_readonly_rejects_unknown_tool():
    with pytest.raises(connector_kinds.KindRejected, match='TOOL_UNKNOWN'):
        connector_kinds.readonly_command({'tool': 'run_anything', 'params': {}})


def test_readonly_refuses_payload_without_params():
    """A payload carrying only program+args cannot be re-derived, so it is
    refused instead of trusted."""
    task = {'tool': 'read_file', 'program': '/usr/bin/cat', 'args': ['/etc/shadow']}
    with pytest.raises(connector_kinds.KindRejected, match='READONLY_PARAMS_REQUIRED'):
        connector_kinds.readonly_command(task)


def test_readonly_rejects_tampered_expansion():
    """Params say one file, argv says another: the wire was edited."""
    task = {'tool': 'read_file', 'params': {'path': '/etc/hostname'},
            'args': ['--number', '--', '/etc/shadow']}
    with pytest.raises(connector_kinds.KindRejected, match='READONLY_EXPANSION_MISMATCH'):
        connector_kinds.readonly_command(task)


@pytest.mark.parametrize('path', ['/etc/passwd\x00', '../etc/shadow', 'relative/path', ''])
def test_readonly_rejects_bad_paths(path):
    with pytest.raises(connector_kinds.KindRejected):
        connector_kinds.readonly_command({'tool': 'read_file', 'params': {'path': path}})


def test_readonly_imports_the_same_tool_table_as_the_control_plane():
    """No second copy of the readonly surface: drift here would mean the
    connector and the main service disagree about what "read only" means."""
    assert connector_kinds.PROGRAMS is readonly_tools.PROGRAMS


def test_readonly_remote_line_has_no_shell_wrapper():
    program, argv = connector_kinds.readonly_command(
        {'tool': 'list_dir', 'params': {'path': '/var/log'}})
    remote = connector_kinds.remote_command(program, argv, run_as=None)
    assert '/bin/sh' not in remote
    assert remote.startswith('/usr/bin/ls ')


def test_readonly_quotes_hostile_arguments():
    """A filename with shell metacharacters must arrive as one argv token."""
    program, argv = readonly_tools.PROGRAMS['read_file'], ['--number', '--', '/tmp/a;rm -rf /']
    remote = connector_kinds.remote_command(program, argv, run_as=None)
    assert ';rm' not in remote.replace("'/tmp/a;rm -rf /'", '')


def test_remote_command_requires_absolute_program():
    with pytest.raises(connector_kinds.KindRejected, match='PROGRAM_NOT_ABSOLUTE'):
        connector_kinds.remote_command('cat', ['/etc/hostname'], run_as=None)


@pytest.mark.parametrize('bad', ['/tmp/x\nid', '/tmp/x\rid', '/tmp/x\x00y'])
def test_remote_command_rejects_control_characters(bad):
    with pytest.raises(connector_kinds.KindRejected, match='ARGV_CONTROL_CHARACTER'):
        connector_kinds.remote_command('/usr/bin/cat', [bad], run_as=None)


# --- change -----------------------------------------------------------------

def _change_task(action='service.restart', params=None, run_as='ops'):
    params = params if params is not None else {'unit': 'nginx.service'}
    step = actions.expand(action, params)
    digest = actions.payload_hash([step], 'asset-1', run_as)
    return {'kind': 'change', 'asset_id': 'asset-1', 'run_as': run_as,
            'steps': [step], 'approval': {'payload_sha256': digest}}


def test_change_accepts_a_hash_that_matches():
    task = _change_task()
    resolved = connector_kinds.change_commands(task)
    assert len(resolved) == 1
    assert resolved[0][1] == '/usr/bin/systemctl'
    assert resolved[0][2] == ['restart', 'nginx.service']


def test_change_rejects_when_argv_was_edited_after_approval():
    task = _change_task()
    task['steps'][0]['args'] = ['restart', 'postgresql.service']
    with pytest.raises(connector_kinds.KindRejected, match='ACTION_EXPANSION_MISMATCH'):
        connector_kinds.change_commands(task)


def test_change_rejects_when_params_edited_after_approval():
    """The 2026-10-22 real-host lesson: params carry the content for
    file.write, so a hash that ignored them would let a body edit pass.

    Note *which* check catches this: the argv is unchanged (only the path is
    in it), so re-expansion still agrees. Only the hash sees the body change --
    which is exactly why params had to go into the hash.
    """
    task = _change_task('file.write', {'path': '/tmp/x', 'content': 'ok'})
    task['steps'][0]['params'] = {'path': '/tmp/x', 'content': 'evil'}
    with pytest.raises(connector_kinds.KindRejected, match='APPROVAL_HASH_MISMATCH'):
        connector_kinds.change_commands(task)


def test_change_rejects_wrong_hash():
    task = _change_task()
    task['approval'] = {'payload_sha256': '0' * 64}
    with pytest.raises(connector_kinds.KindRejected, match='APPROVAL_HASH_MISMATCH'):
        connector_kinds.change_commands(task)


def test_change_rejects_missing_approval():
    task = _change_task()
    task.pop('approval')
    with pytest.raises(connector_kinds.KindRejected, match='APPROVAL_HASH_MISMATCH'):
        connector_kinds.change_commands(task)


def test_change_rejects_hash_bound_to_another_host():
    """Approving on host A must not authorise running on host B."""
    task = _change_task()
    task['asset_id'] = 'asset-2'
    with pytest.raises(connector_kinds.KindRejected, match='APPROVAL_HASH_MISMATCH'):
        connector_kinds.change_commands(task)


def test_change_rejects_unknown_action():
    task = {'steps': [{'action': 'rm.rf', 'params': {}}], 'approval': {}}
    with pytest.raises(connector_kinds.KindRejected, match='ACTION_UNKNOWN'):
        connector_kinds.change_commands(task)


def test_change_rejects_oversized_set():
    task = _change_task()
    task['steps'] = task['steps'] * (actions.MAX_ACTIONS + 1)
    with pytest.raises(connector_kinds.KindRejected, match='CHANGE_SET_SIZE'):
        connector_kinds.change_commands(task)


def test_change_rejects_baseline_denied_path():
    step = {'action': 'file.write',
            'params': {'path': '/etc/shadow', 'content': 'x'}}
    task = {'asset_id': 'a', 'run_as': 'ops', 'steps': [step], 'approval': {}}
    with pytest.raises(connector_kinds.KindRejected):
        connector_kinds.change_commands(task)


def test_change_runs_steps_in_order_and_quotes_them():
    first = actions.expand('service.restart', {'unit': 'a.service'})
    second = actions.expand('service.reload', {'unit': 'b.service'})
    digest = actions.payload_hash([first, second], 'asset-1', 'ops')
    task = {'kind': 'change', 'asset_id': 'asset-1', 'run_as': 'ops',
            'steps': [first, second], 'approval': {'payload_sha256': digest}}
    resolved = connector_kinds.change_commands(task)
    assert [step['action'] for step, _, _ in resolved] == ['service.restart', 'service.reload']
