"""Gate 1/2 readonly channel, server side (docs/readonly-and-authorization.md §4, §39.2)."""
import json

import pytest

from ai_ops import readonly_tools
from ai_ops.readonly_tools import ReadonlyRejected, build


def test_read_file_is_structured_and_numbered():
    program, argv = build('read_file', {'path': '/etc/nginx/nginx.conf'})
    assert program == '/usr/bin/cat'
    assert argv == ['--number', '--', '/etc/nginx/nginx.conf']


def test_path_normalization_is_lexical_and_root_anchored():
    assert build('read_file', {'path': '/etc/./nginx/../nginx/nginx.conf'})[1][-1] == '/etc/nginx/nginx.conf'
    with pytest.raises(ReadonlyRejected):
        build('read_file', {'path': '/../../etc/passwd'})


@pytest.mark.parametrize('path', ['etc/passwd', '', 'relative/path', '/etc/pass\nwd'])
def test_relative_and_malformed_paths_rejected(path):
    with pytest.raises(ReadonlyRejected):
        build('read_file', {'path': path})


@pytest.mark.parametrize('tool,params', [
    ('read_file', {'path': '/etc/passwd'}),
    ('list_dir', {'path': '/tmp'}),
    ('file_stat', {'path': '/tmp'}),
    ('disk_usage', {'path': '/'}),
])
def test_no_tool_ever_produces_output_redirection_argv(tool, params):
    program, argv = build(tool, params)
    assert program.startswith('/usr/')
    for arg in argv:
        assert not arg.lower().startswith(('-fprintf', '--output', '-o'))


def test_unknown_tool_is_rejected():
    with pytest.raises(ReadonlyRejected):
        build('run_shell', {'command': 'id'})


def test_service_status_only_readonly_verbs():
    program, argv = build('service_status', {'unit': 'nginx.service'})
    assert program == '/usr/bin/systemctl'
    assert 'start' not in argv and 'stop' not in argv and 'restart' not in argv
    with pytest.raises(ReadonlyRejected):
        build('service_status', {'unit': 'nginx; reboot'})


def test_journal_logs_has_bounded_line_count():
    assert build('journal_logs', {'unit': 'nginx', 'lines': 5})[1][:3] == ['--no-pager', '-n', '5']
    with pytest.raises(ReadonlyRejected):
        build('journal_logs', {'lines': 999999})


def test_kubectl_surface_is_get_and_logs_only():
    program, argv = build('k8s_get', {'kind': 'pods', 'namespace': 'kube-system'})
    assert program == '/usr/bin/kubectl' and argv[:2] == ['get', 'pods']
    program, argv = build('k8s_logs', {'name': 'web-0', 'namespace': 'prod', 'lines': 10})
    assert argv[:2] == ['logs', 'web-0']
    with pytest.raises(ReadonlyRejected):
        build('k8s_get', {'kind': 'pods --all-namespaces'})


def test_tool_spec_requires_asset_and_account_on_every_tool():
    specs = readonly_tools.tool_spec()
    names = [s['function']['name'] for s in specs]
    assert 'execute_command' not in names
    for spec in specs:
        params = spec['function']['parameters']
        assert params['additionalProperties'] is False
        assert set(('asset_id', 'run_as')) <= set(params['required'])
        # No free-form command/args escape hatch anywhere in the surface.
        assert 'command' not in params['properties']
        assert 'args' not in params['properties']
