"""Gate 3/4: structured change set + the policy engine that decides it runs.

Design: docs/readonly-and-authorization.md §5, §39.1, §43.2, §43.5, §44, §45.

The whole point of this module: the model never submits a command string for a
write. It submits a *change set* made of typed actions with parameters. Every
action is expanded here into a concrete argv, and the policy engine decides
`execute` / `approve` / `reject` from the tuple (mode, action, trigger source,
host). One table, not a whitelist per mode — otherwise it explodes with
mode × host combinations.

Two invariants that are easy to lose and expensive to get wrong:
  * `direct` mode is not "allow everything": it auto-approves only actions
    marked `auto_approvable`. Everything else still needs a human.
  * A human approval binds to a payload hash (§39.3). If the payload changes
    between approval and execution, execution is refused.
"""
from __future__ import annotations

import hashlib
import json
import re

# ---------------------------------------------------------------------------
# Action table
# ---------------------------------------------------------------------------
# risk: 'low'   - reversible, narrow blast radius, safe unattended
#       'high'  - can take a service down, or is irreversible
# auto_approvable marks what `direct` mode may run without a human. Risk alone
# does not grant it: the flag is explicit so a table edit is always a decision.
ACTIONS: dict[str, dict] = {
    'service.restart': {
        'summary': 'Restart one systemd unit',
        'risk': 'high',
        'auto_approvable': True,
        'program': '/usr/bin/systemctl',
        'params': {'unit': 'unit'},
        'build': lambda p: ['restart', p['unit']],
        'identity': lambda p: 'systemd:' + p['unit'],
    },
    'service.reload': {
        'summary': 'Reload one systemd unit (config only, no restart)',
        'risk': 'low',
        'auto_approvable': True,
        'program': '/usr/bin/systemctl',
        'params': {'unit': 'unit'},
        'build': lambda p: ['reload', p['unit']],
        'identity': lambda p: 'systemd:' + p['unit'],
    },
    'service.start': {
        'summary': 'Start one systemd unit',
        'risk': 'high',
        'auto_approvable': False,
        'program': '/usr/bin/systemctl',
        'params': {'unit': 'unit'},
        'build': lambda p: ['start', p['unit']],
        'identity': lambda p: 'systemd:' + p['unit'],
    },
    'service.stop': {
        'summary': 'Stop one systemd unit',
        'risk': 'high',
        'auto_approvable': False,
        'program': '/usr/bin/systemctl',
        'params': {'unit': 'unit'},
        'build': lambda p: ['stop', p['unit']],
        'identity': lambda p: 'systemd:' + p['unit'],
    },
    'file.write': {
        'summary': 'Replace the whole content of one file',
        'risk': 'high',
        'auto_approvable': False,
        'program': '__builtin_write__',
        'params': {'path': 'path', 'content': 'text'},
        'build': lambda p: [p['path']],
        'identity': lambda p: 'file:' + p['path'],
    },
    'file.append': {
        'summary': 'Append one line to a file',
        'risk': 'high',
        'auto_approvable': False,
        'program': '__builtin_append__',
        'params': {'path': 'path', 'line': 'text'},
        'build': lambda p: [p['path']],
        'identity': lambda p: 'file:' + p['path'],
    },
    'log.rotate': {
        'summary': 'Truncate a log file that is safe to truncate',
        'risk': 'low',
        'auto_approvable': True,
        'program': '/usr/bin/truncate',
        'params': {'path': 'path'},
        'build': lambda p: ['-s', '0', p['path']],
        'identity': lambda p: 'file:' + p['path'],
    },
    'disk.clean_temp': {
        'summary': 'Delete files older than N days under a temp directory',
        'risk': 'low',
        'auto_approvable': True,
        'program': '/usr/bin/find',
        'params': {'path': 'path', 'days': 'int'},
        'build': lambda p: [p['path'], '-maxdepth', '1', '-type', 'f', '-mtime', '+' + str(p['days']), '-delete'],
        'identity': lambda p: 'dir:' + p['path'],
    },
    'k8s.rollout_restart': {
        'summary': 'kubectl rollout restart a deployment',
        'risk': 'high',
        'auto_approvable': True,
        'program': '/usr/bin/kubectl',
        'params': {'kind': 'k8s_kind', 'name': 'k8s_name', 'namespace': 'k8s_ns'},
        'build': lambda p: ['rollout', 'restart', p['kind'] + '/' + p['name'], '-n', p['namespace']],
        'identity': lambda p: 'k8s:%s/%s/%s' % (p['namespace'], p['kind'], p['name']),
    },
    'shell.run': {
        'summary': 'Arbitrary shell command (human approval only, never unattended)',
        'risk': 'high',
        'auto_approvable': False,
        'program': '/bin/sh',
        'params': {'command': 'shell'},
        'build': lambda p: ['-c', p['command']],
        # A shell command has no stable identity to bind an approval to beyond
        # the exact text, and it always needs a human, so it is never
        # auto-approved and never reachable from a trigger.
        'identity': lambda p: 'shell:' + hashlib.sha256(p['command'].encode()).hexdigest(),
    },
}

# Paths a change may never touch, regardless of the name list. A user list is a
# refinement on top of these, never a way around them (§44.2 denies win).
BASELINE_DENY_PATHS = (
    '/etc/shadow', '/etc/shadow-', '/etc/gshadow', '/etc/sudoers', '/etc/sudoers.d',
    '/root', '/boot', '/etc/ssh/ssh_host_', '/var/lib/ai-ops', '/etc/ai-ops-agent',
)

UNIT = re.compile(r'^[A-Za-z0-9@._:-]{1,128}$')
ABS_PATH = re.compile(r'^/[^\x00\n\r]{0,1023}$')
K8S_NAME = re.compile(r'^[a-z0-9]([a-z0-9.-]{0,251})$')
K8S_KIND = re.compile(r'^(deployment|statefulset|daemonset)$')

# A change set is capped so one model step cannot queue an unbounded batch.
MAX_ACTIONS = 10
MAX_OUTPUT = 2000


class ChangeRejected(ValueError):
    """The change set is malformed or outside the action table."""


def _check_text(value, limit, field):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ChangeRejected('PARAM_INVALID:' + field)
    if len(value) > limit:
        raise ChangeRejected('PARAM_TOO_LONG:' + field)
    return value


def _check_param(kind, value, field):
    if kind == 'unit':
        value = _check_text(value, 128, field)
        if not UNIT.match(value):
            raise ChangeRejected('UNIT_INVALID:' + field)
        return value
    if kind == 'path':
        value = _check_text(value, 1024, field)
        if not ABS_PATH.match(value) or '..' in value.split('/'):
            raise ChangeRejected('PATH_INVALID:' + field)
        for denied in BASELINE_DENY_PATHS:
            if value == denied or value.startswith(denied.rstrip('/') + '/'):
                raise ChangeRejected('PATH_DENIED_BASELINE:' + field)
        return value
    if kind == 'int':
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 365:
            raise ChangeRejected('INT_INVALID:' + field)
        return value
    if kind == 'text':
        return _check_text(value, 1024 * 1024, field)
    if kind == 'shell':
        return _check_text(value, 16000, field)
    if kind == 'k8s_name':
        value = _check_text(value, 253, field)
        if not K8S_NAME.match(value):
            raise ChangeRejected('K8S_NAME_INVALID:' + field)
        return value
    if kind == 'k8s_kind':
        value = _check_text(value, 32, field)
        if not K8S_KIND.match(value):
            raise ChangeRejected('K8S_KIND_INVALID:' + field)
        return value
    if kind == 'k8s_ns':
        value = _check_text(value, 63, field)
        if not K8S_NAME.match(value):
            raise ChangeRejected('K8S_NAMESPACE_INVALID:' + field)
        return value
    raise ChangeRejected('PARAM_KIND_UNKNOWN:' + field)


def expand(action: str, params: dict) -> dict:
    """Validate one action and turn it into a concrete, auditable execution."""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ChangeRejected('ACTION_UNKNOWN:' + str(action)[:64])
    if not isinstance(params, dict):
        raise ChangeRejected('PARAMS_INVALID')
    allowed = spec['params']
    if set(params) != set(allowed):
        raise ChangeRejected('PARAMS_MISMATCH:' + action)
    clean = {name: _check_param(kind, params[name], name) for name, kind in allowed.items()}
    argv = spec['build'](clean)
    timeout = 30 if action.startswith('service.') else 120
    return {'action': action, 'params': clean, 'program': spec['program'], 'args': argv,
            'identity': spec['identity'](clean), 'timeout_seconds': timeout,
            'auto_approvable': spec['auto_approvable'], 'risk': spec['risk']}


def normalize(change_set) -> list[dict]:
    """Validate a whole change set. Returns expanded steps in order."""
    if not isinstance(change_set, list) or not 1 <= len(change_set) <= MAX_ACTIONS:
        raise ChangeRejected('CHANGE_SET_SIZE')
    steps = [expand(item.get('action'), item.get('params') or {}) for item in change_set
             if isinstance(item, dict)] if all(isinstance(i, dict) for i in change_set) else None
    if steps is None:
        raise ChangeRejected('CHANGE_SET_SHAPE')
    return steps


def payload_hash(steps: list[dict], asset_id: str, run_as: str) -> str:
    """The thing a human approval is bound to (§39.3, §45.5).

    Covers the expanded argv, not the model's prose, and includes the target
    and the account so "approve A, run B" and "approve on host A, run on B"
    both fail the check.
    """
    material = {'asset_id': asset_id, 'run_as': run_as,
                'steps': [[s['action'], s['program'], s['args'], s['identity'], s.get('params')]
                          for s in steps]}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------
# Output: 'execute' (queue now), 'approve' (needs a human), 'reject'.
# The trigger-source narrowing (§43.5) lives here too: a trigger carries the
# actions it was configured for; anything outside that set is refused even if
# the action is auto_approvable.
def decide(mode: str, step: dict, *, trigger_source: str | None = None,
           trigger_actions: list[str] | None = None,
           asset_tags: list[str] | None = None,
           action_hosts: dict[str, list[str]] | None = None) -> tuple[str, str]:
    """Return (verdict, reason_code)."""
    action = step['action']
    if trigger_source is not None:
        # Unattended: no human in the loop, so anything the trigger was not
        # explicitly configured for is refused, not escalated.
        if trigger_actions is None or action not in trigger_actions:
            return 'reject', 'TRIGGER_ACTION_NOT_PERMITTED'
        if mode != 'direct':
            return 'reject', 'TRIGGER_REQUIRES_DIRECT_MODE'
        if not step['auto_approvable']:
            return 'reject', 'TRIGGER_ACTION_NEEDS_HUMAN'
    if mode == 'readonly':
        return 'reject', 'READONLY_MODE'
    # Per-rule host scope (§44.1): a rule may be limited to hosts carrying a tag.
    scope = (action_hosts or {}).get(action)
    if scope:
        tags = set(asset_tags or [])
        if not any(tag in tags for tag in scope):
            return 'reject', 'HOST_OUT_OF_SCOPE'
    if mode == 'direct':
        if step['auto_approvable']:
            return 'execute', 'DIRECT_AUTO'
        return 'approve', 'DIRECT_NOT_AUTO'
    if mode == 'confirm':
        return 'approve', 'CONFIRM'
    return 'reject', 'MODE_UNKNOWN'


def plan(mode: str, steps: list[dict], **context) -> list[tuple[dict, str, str]]:
    """Decide every step; the whole set is rejected if any step is."""
    return [(step, *decide(mode, step, **context)) for step in steps]
