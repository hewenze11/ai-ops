"""Read-only channel (gate 1/2) — server side.

Design: docs/readonly-and-authorization.md §4, §39.2, §43.2, §48;
protocol: docs/protocol-v13.md.

A readonly task never carries a shell string. It carries a *tool* the model
chose from a closed set plus structured parameters. Only this module turns a
tool+params into `program` + `argv`, and the Agent re-validates the same
program+argv independently at execve time. Two independent checks, no
`sh -c` anywhere on this path.

Nothing here grants write access: every tool maps to a program that cannot
modify files, and any argv that could redirect output is rejected.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# Absolute paths only: a bare name would be resolved through PATH at the
# endpoint, which is exactly the indirection we refuse to allow.
PROGRAMS: dict[str, str] = {
    'read_file': '/usr/bin/cat',
    'list_dir': '/usr/bin/ls',
    'file_stat': '/usr/bin/stat',
    'disk_usage': '/usr/bin/df',
    'memory_info': '/usr/bin/free',
    'process_list': '/usr/bin/ps',
    'service_status': '/usr/bin/systemctl',
    'journal_logs': '/usr/bin/journalctl',
    'k8s_get': '/usr/bin/kubectl',
    'k8s_logs': '/usr/bin/kubectl',
}

# Tools that may only ever run `status`/`cat`-style verbs.
SYSTEMCTL_VERBS = ('status', 'is-active', 'is-failed', 'is-enabled', 'show')
KUBECTL_VERBS = ('get', 'describe', 'logs', 'top', 'version')

# Interpreter-ish programs are never allowed, even transitively: they turn any
# argv into "run anything". This is the list the Agent also enforces.
FORBIDDEN_PROGRAMS = frozenset({
    'sh', 'bash', 'dash', 'zsh', 'ksh', 'csh', 'tcsh', 'fish',
    'python', 'python3', 'perl', 'ruby', 'node', 'php', 'lua',
    'awk', 'gawk', 'mawk', 'sed', 'xargs', 'env', 'find', 'tee',
    'curl', 'wget', 'nc', 'ncat', 'netcat', 'socat', 'ssh', 'scp', 'rsync',
    'less', 'more', 'vi', 'vim', 'nano', 'ed',
})

# Any argv element starting with one of these can write files or spawn
# helpers; reject before it reaches the endpoint. Kept program-agnostic on
# purpose: the few false positives are worth failing closed on.
FORBIDDEN_ARG_PREFIXES = (
    '-fprintf', '-fprint', '-fls', '--output', '--log-file', '--output-',
    '--exec', '-exec', '-execdir', '--compress-program', '--to-command',
)
# Short flags are program-specific: '-o' redirects output in sort/uniq but is
# plain "output format" in kubectl and df does not take either. Narrow the
# blanket rejection and check these per program instead.
FORBIDDEN_ARG_PREFIXES_BY_PROGRAM = {
    'sort': ('-o', '-t', '--output'),
    'uniq': ('--output',),
    'ls': ('-w',),
}

UNIT = re.compile(r'^[A-Za-z0-9@._\\:-]{1,128}$')
PATH = re.compile(r'^/[^\x00\n\r]{0,1023}$')
K8S_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.:/-]{0,127}$')

# Both fields are mandatory on every readonly tool call: a read is always
# "this account on this asset". The turn's grants decide whether it is legal.
ASSET = {"type": "string", "description": "Registered asset id."}
RUN_AS = {"type": "string", "description": "Native account selected for this turn."}


class ReadonlyRejected(ValueError):
    """The requested read is outside the closed readonly surface."""


def _normalize(path: str) -> str:
    """Lexical normalization. The Agent additionally resolves symlinks with
    realpath right before execve; this server-side pass keeps the two in sync
    for the common case and makes audits readable."""
    if not isinstance(path, str) or not PATH.match(path):
        raise ReadonlyRejected('PATH_INVALID')
    parts: list[str] = []
    for piece in PurePosixPath(path).parts:
        if piece == '/':
            continue
        if piece == '..':
            if not parts:
                raise ReadonlyRejected('PATH_ESCAPES_ROOT')
            parts.pop()
        elif piece != '.':
            parts.append(piece)
    return '/' + '/'.join(parts)


# Exact argv tokens that are known-safe even though they look like output
# redirection. Everything else matching the patterns below is rejected.
SAFE_ARGV_TOKENS = frozenset({'--number', '--output=short-iso', 'wide'})


def _check_argv(program: str, argv: list[str]) -> None:
    base = PurePosixPath(program).name
    if base in FORBIDDEN_PROGRAMS:
        raise ReadonlyRejected('PROGRAM_FORBIDDEN')
    extra = FORBIDDEN_ARG_PREFIXES_BY_PROGRAM.get(base, ())
    for arg in argv:
        if not isinstance(arg, str) or '\x00' in arg or '\n' in arg:
            raise ReadonlyRejected('ARG_INVALID')
        low = arg.lower()
        if arg in SAFE_ARGV_TOKENS:
            continue
        if any(low.startswith(prefix) for prefix in FORBIDDEN_ARG_PREFIXES + extra):
            raise ReadonlyRejected('ARG_WRITES_OUTPUT')


def build(tool: str, params: dict) -> tuple[str, list[str]]:
    """Map a model-facing readonly tool call to (program, argv).

    Raises ReadonlyRejected for anything outside the closed set. Never returns
    a shell string, and never passes user text through a shell.
    """
    params = params or {}
    if tool == 'read_file':
        path = _normalize(str(params.get('path') or ''))
        program, argv = PROGRAMS[tool], ['--number', '--', path]
    elif tool == 'list_dir':
        path = _normalize(str(params.get('path') or '/'))
        long_form = bool(params.get('long'))
        program = PROGRAMS[tool]
        argv = (['-la', '--'] if long_form else ['-a', '--']) + [path]
    elif tool == 'file_stat':
        path = _normalize(str(params.get('path') or ''))
        program, argv = PROGRAMS[tool], ['--', path]
    elif tool == 'disk_usage':
        # df does not take '--'; the normalized path never starts with '-'.
        path = _normalize(str(params.get('path') or '/'))
        program, argv = PROGRAMS[tool], ['-h', path]
    elif tool == 'memory_info':
        program, argv = PROGRAMS[tool], ['-m']
    elif tool == 'process_list':
        program, argv = PROGRAMS[tool], ['-eo', 'pid,ppid,user,pcpu,pmem,etime,args']
    elif tool == 'service_status':
        unit = str(params.get('unit') or '')
        if not UNIT.match(unit):
            raise ReadonlyRejected('UNIT_INVALID')
        program, argv = PROGRAMS[tool], ['--no-pager', '--full', 'status', unit]
    elif tool == 'journal_logs':
        unit = params.get('unit')
        lines = params.get('lines', 200)
        if not isinstance(lines, int) or not 1 <= lines <= 2000:
            raise ReadonlyRejected('LINES_INVALID')
        argv = ['--no-pager', '-n', str(lines)]
        if unit is not None:
            if not UNIT.match(str(unit)):
                raise ReadonlyRejected('UNIT_INVALID')
            argv += ['-u', str(unit)]
        program, argv = PROGRAMS[tool], argv + ['--output=short-iso']
    elif tool in ('k8s_get', 'k8s_logs'):
        namespace = str(params.get('namespace') or 'default')
        if not K8S_NAME.match(namespace):
            raise ReadonlyRejected('NAMESPACE_INVALID')
        program = PROGRAMS[tool]
        if tool == 'k8s_get':
            kind = str(params.get('kind') or '')
            if not K8S_NAME.match(kind):
                raise ReadonlyRejected('KIND_INVALID')
            name = params.get('name')
            argv = ['get', kind, '-n', namespace, '-o', 'wide']
            if name is not None:
                if not K8S_NAME.match(str(name)):
                    raise ReadonlyRejected('NAME_INVALID')
                argv.insert(2, str(name))
        else:
            name = params.get('name')
            if name is None or not K8S_NAME.match(str(name)):
                raise ReadonlyRejected('NAME_INVALID')
            lines = params.get('lines', 200)
            if not isinstance(lines, int) or not 1 <= lines <= 2000:
                raise ReadonlyRejected('LINES_INVALID')
            argv = ['logs', str(name), '-n', namespace, '--tail', str(lines)]
    else:
        raise ReadonlyRejected('TOOL_UNKNOWN')
    _check_argv(program, argv)
    return program, argv


def tool_spec() -> list[dict]:
    """The readonly tool surface handed to the model.

    Deliberately narrow: no generic 'run this command' escape hatch, because
    that is the whole point of gate 1 (see §43.2). Each tool carries the target
    asset and the native account so the server can bind the call to a turn's
    granted accounts, exactly like execute_command does.
    """
    def fn(name, description, properties, required):
        all_props = {'asset_id': ASSET, 'run_as': RUN_AS, **properties}
        return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": all_props, "required": ['asset_id', 'run_as'] + required}}}
    path = {"type": "string", "description": "Absolute path on the target host."}
    unit = {"type": "string", "description": "systemd unit name, e.g. nginx.service"}
    lines = {"type": "integer", "minimum": 1, "maximum": 2000}
    ns = {"type": "string", "description": "Kubernetes namespace."}
    return [
        fn('read_file', 'Read a text file (with line numbers). Secret values are masked before you see them.', {'path': path}, ['path']),
        fn('list_dir', 'List a directory.', {'path': path, 'long': {'type': 'boolean'}}, ['path']),
        fn('file_stat', 'Show metadata for one path.', {'path': path}, ['path']),
        fn('disk_usage', 'Filesystem usage for a path.', {'path': path}, ['path']),
        fn('memory_info', 'Memory usage summary.', {}, []),
        fn('process_list', 'All processes with pid, user, cpu, mem, elapsed, args.', {}, []),
        fn('service_status', 'systemd unit status. Read-only verbs only.', {'unit': unit}, ['unit']),
        fn('journal_logs', 'Recent systemd journal lines, optionally for one unit.', {'unit': unit, 'lines': lines}, []),
        fn('k8s_get', 'kubectl get <kind> [-n ns] [-o wide], optionally one object.', {'kind': {'type': 'string'}, 'name': {'type': 'string'}, 'namespace': ns}, ['kind']),
        fn('k8s_logs', 'kubectl logs <pod> --tail N.', {'name': {'type': 'string'}, 'namespace': ns, 'lines': lines}, ['name']),
    ]
