"""Execution kinds over the SSH connector (no Agent installed).

Why this exists
---------------
`connector_ssh` historically ran a shell string on the target. That made every
ssh-only asset a hole straight through gates 1/3: no closed readonly surface,
no argv re-check at the endpoint, no approval hash binding.

This module closes that hole *without* an Agent on the target, by moving the
"endpoint check" to the only endpoint that exists -- the control plane -- and
narrowing what it can express:

* **readonly**: the model picks a tool + params; argv is derived here from the
  closed table in ``readonly_tools``; nothing is ever passed through a shell.
* **change**: steps come from ``actions``; before running, each step's
  program+argv is re-derived from its params and the approval hash is
  recomputed. A tampered payload fails closed.

Honest limitation (documented, not hidden)
-----------------------------------------
On an Agent asset there are **two independent** validators: control plane and
Agent. Here there is only one, because there is no code on the target. An
attacker who owns this process can therefore run whatever the SSH account can
run. That is precisely why:

* the SSH account must be low-privilege (same rule as the Agent's readonly
  account), and
* ``sudo`` is used only for the exact units listed in ``--ops-units``.

This is a real reduction in assurance versus an Agent asset, and it is the
reason the product recommends the Agent when a channel must be defended against
its own control plane (§51.1). The connector path is for "easy first contact",
not for the highest-assurance deployments.
"""
from __future__ import annotations

import shlex

from . import actions, readonly_tools

# Reuse the readonly tool table: the connector must not have a second, drifting
# copy of what "read only" means.
PROGRAMS = readonly_tools.PROGRAMS

# A readonly task over SSH runs as the ssh login account. We never `sudo` a
# read: elevating a read is exactly the escalation this channel forbids.
MAX_ARGV = 64


class KindRejected(ValueError):
    """The task cannot be expressed safely on the connector path."""


def _quote_argv(argv: list[str]) -> str:
    """Quote argv for a *non-shell* remote exec.

    The remote command is a single line because that is all SSH exec gives us,
    so the quoting must be exact. shlex.quote is POSIX-correct for the target
    shell; we additionally reject control characters so nothing can smuggle a
    newline into the command line.
    """
    out = []
    for item in argv:
        if not isinstance(item, str) or '\x00' in item or '\n' in item or '\r' in item:
            raise KindRejected('ARGV_CONTROL_CHARACTER')
        out.append(shlex.quote(item))
    return ' '.join(out)


def readonly_command(task: dict) -> tuple[str, list[str]]:
    """Derive (program, argv) for a readonly task, refusing anything unknown.

    The wire carries tool+params only in principle, but we re-derive from
    params rather than trusting ``program``/``args``, mirroring the Agent's
    independent check. If the wire disagrees with what the table produces, the
    payload was tampered with.
    """
    tool = task.get('tool')
    params = task.get('params')
    if not isinstance(tool, str) or tool not in PROGRAMS:
        raise KindRejected('TOOL_UNKNOWN')
    if not isinstance(params, dict):
        # Older payloads carried program+args; we refuse rather than accept an
        # argv we cannot re-derive.
        raise KindRejected('READONLY_PARAMS_REQUIRED')
    try:
        program, argv = readonly_tools.build(tool, params)
    except readonly_tools.ReadonlyRejected as error:
        raise KindRejected(str(error)) from None
    for key, want in (('program', program), ('args', argv)):
        if task.get(key) is not None and task.get(key) != want:
            # Fail closed on any disagreement. This is the connector's version
            # of the Agent's independent re-derivation.
            raise KindRejected('READONLY_EXPANSION_MISMATCH')
    if len(argv) > MAX_ARGV:
        raise KindRejected('ARGV_TOO_LONG')
    return program, argv


def change_commands(task: dict) -> list[tuple[dict, str, list[str]]]:
    """Validate a change set and return [(step, program, argv)] to run in order.

    Three checks, all of which the Agent performs locally; here they must
    happen before the SSH call because they are the only gate that exists:
    1. every action is in this build's table and its params still re-expand to
       the same program+argv (tamper / drift detection),
    2. the approval hash still covers exactly this payload (a human approved a
       specific byte sequence, not a name),
    3. no step's argv carries a control character that could re-parse remotely.
    """
    steps = task.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= actions.MAX_ACTIONS:
        raise KindRejected('CHANGE_SET_SIZE')
    resolved: list[tuple[dict, str, list[str]]] = []
    for step in steps:
        if not isinstance(step, dict):
            raise KindRejected('CHANGE_SET_SHAPE')
        action = step.get('action')
        spec = actions.ACTIONS.get(action)
        if spec is None:
            raise KindRejected('ACTION_UNKNOWN:' + str(action)[:64])
        params = step.get('params')
        if not isinstance(params, dict):
            raise KindRejected('ACTION_PARAMS_INVALID')
        try:
            rebuilt = actions.expand(action, params)
        except actions.ChangeRejected as error:
            raise KindRejected(str(error)) from None
        if step.get('program') != rebuilt['program'] or step.get('args') != rebuilt['args']:
            raise KindRejected('ACTION_EXPANSION_MISMATCH:' + action)
        if step.get('identity') != rebuilt['identity']:
            raise KindRejected('ACTION_IDENTITY_MISMATCH:' + action)
        _quote_argv(rebuilt['args'])  # reject control characters early
        resolved.append((step, rebuilt['program'], rebuilt['args']))
    # Recompute the hash over exactly the fields the control plane approved.
    digest = actions.payload_hash(
        [dict(step, program=program, args=argv) for step, program, argv in resolved],
        task.get('asset_id'), task.get('run_as'))
    approved = (task.get('approval') or {}).get('payload_sha256')
    if not isinstance(approved, str) or approved != digest:
        raise KindRejected('APPROVAL_HASH_MISMATCH')
    return resolved


def remote_command(program: str, argv: list[str], run_as: str, sudo_units: tuple[str, ...] = ()) -> str:
    """Build the single remote command line.

    ``run_as`` is the SSH login account itself for reads; for changes that need
    privilege we use ``sudo -n -u <account>`` only when the account differs,
    and ``sudo -n`` for systemctl on an allow-listed unit is handled by
    sudoers -- we never synthesise a broader sudo than sudoers already grants.
    """
    if not program.startswith('/'):
        raise KindRejected('PROGRAM_NOT_ABSOLUTE')
    body = _quote_argv([program] + argv)
    if run_as and run_as != 'root':
        # -n: never prompt. If sudoers does not allow it, the task fails loudly
        # instead of hanging on a password prompt.
        return 'sudo -n -u %s -- %s' % (shlex.quote(run_as), body)
    return body
