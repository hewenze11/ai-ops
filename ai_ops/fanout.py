"""Fan-out reads: one model tool call, N hosts, one aligned answer (§52.4).

Why this exists
---------------
Diagnosing an outage across 100 hosts one command at a time is not just slow,
it is wrong: host 1 and host 100 are read tens of minutes apart, so the timeline
the model reconstructs is a fiction. The fix is to fan out, but *not* by letting
the model emit several tool calls:

* several tool calls multiply approvals, audit rows and ordering questions;
* the results arrive as N scattered blobs the model has to stitch together,
  which is exactly the context blow-up we are trying to avoid;
* per-call authority checks would need to agree with each other, and no one
  would be able to say what "the turn's authority" meant any more.

A fan-out is one tool call, one name-list snapshot, one audit trail, and one
aligned result. Concurrency stays a control-plane decision (§52.3): the model
chooses *what* to read and *where*, never *how many at once*.

What this module is
-------------------
Pure planning and assembly. It does not execute anything and does not touch the
network: it validates the request, splits it into per-asset sub-tasks, and turns
finished sub-tasks back into one result. Execution lives in `fanout_runner`.
"""
from __future__ import annotations

import json
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Per call. Above this the answer stops fitting in a model context: 100 hosts x
# 2KB of log is 200KB, and a result nobody can read is not a result.
MAX_ASSETS_PER_CALL = 50
# Dispatch width. A control-plane setting, not a model knob: the control plane
# knows its connection pool and its own CPU, the model knows neither.
MAX_CONCURRENT_DISPATCH = 10
# Of MAX_ASSETS_PER_CALL. A host that never answers must not hold the whole
# fan-out open until the per-task timeout of every straggler expires.
FANOUT_BUDGET_SECONDS = 180
# Bytes of one host's output kept in the aligned answer.
PER_ASSET_CHARS = 2000
# Total text across all hosts in one answer. A fan-out that returns 200KB of
# logs is not a result, it is a context overflow.
TOTAL_TEXT_CHARS = 24000

SCHEMA = """
CREATE TABLE IF NOT EXISTS fanout_groups(
 id TEXT PRIMARY KEY,
 turn_id TEXT,
 tool TEXT NOT NULL,
 params TEXT NOT NULL,
 asset_ids TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'running',
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 deadline REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fanout_children(
 group_id TEXT NOT NULL REFERENCES fanout_groups(id),
 asset_id TEXT NOT NULL,
 task_id TEXT,
 state TEXT NOT NULL DEFAULT 'pending',
 error_code TEXT,
 created_at REAL NOT NULL,
 PRIMARY KEY(group_id, asset_id));
CREATE INDEX IF NOT EXISTS fanout_children_task ON fanout_children(task_id);
"""


def migrate(db):
    db.executescript(SCHEMA)


class FanoutRequest(BaseModel):
    """What the model is allowed to ask for. Anything else is a rejection."""

    model_config = ConfigDict(extra='forbid')
    asset_ids: list[str] = Field(min_length=1)
    run_as: str = Field(min_length=1, max_length=64)

    @field_validator('asset_ids')
    @classmethod
    def _bounded(cls, value):
        if len(value) > MAX_ASSETS_PER_CALL:
            # Refused, not silently truncated: a model that believes it read 100
            # hosts and actually read 50 will reason confidently about a
            # half-populated picture.
            raise ValueError('TOO_MANY_ASSETS')
        if len(set(value)) != len(value):
            raise ValueError('DUPLICATE_ASSETS')
        return value


class FanoutRejected(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def plan(req, tool, params, asset_lookup):
    """Build the per-asset plan, or raise FanoutRejected.

    ``asset_lookup(asset_id)`` returns the asset row or None. Choosing to keep
    this a callback rather than taking a db handle keeps the module testable
    without a database, which matters because its job is pure decisions.
    """
    from .readonly_tools import ReadonlyRejected, build

    if tool not in ALLOWED_TOOLS:
        raise FanoutRejected('TOOL_NOT_FANOUT_ELIGIBLE')
    # Expand once, here, to prove the parameters are valid for this tool before
    # anything is queued. A bad parameter set should fail the call, not produce
    # 50 identically broken sub-tasks.
    try:
        build(tool, params)
    except ReadonlyRejected as error:
        raise FanoutRejected('PARAMS_INVALID:' + str(error))

    chosen, rejected = [], []
    for asset_id in req.asset_ids:
        asset = asset_lookup(asset_id)
        if asset is None or asset.get('retired_at') is not None:
            rejected.append({'asset_id': asset_id, 'reason': 'ASSET_UNAVAILABLE'})
            continue
        if req.run_as not in json.loads(asset.get('allowed_users') or '[]'):
            rejected.append({'asset_id': asset_id, 'reason': 'ACCOUNT_NOT_ALLOWED'})
            continue
        chosen.append(asset_id)
    if not chosen:
        raise FanoutRejected('NO_ELIGIBLE_ASSETS')
    return chosen, rejected


ALLOWED_TOOLS = frozenset({
    'read_file', 'list_dir', 'file_stat', 'disk_usage', 'memory_info',
    'process_list', 'service_status', 'journal_logs', 'k8s_get', 'k8s_logs',
})


def create(db, *, group_id, turn_id, tool, params, asset_ids, now=None):
    """Record the group and one child row per asset, all pending."""
    now = now or time.time()
    db.execute('INSERT INTO fanout_groups(id,turn_id,tool,params,asset_ids,state,'
               'created_at,updated_at,deadline) VALUES(?,?,?,?,?,?,?,?,?)',
               (group_id, turn_id, tool, json.dumps(params, ensure_ascii=False),
                json.dumps(asset_ids), 'running', now, now,
                now + FANOUT_BUDGET_SECONDS))
    for asset_id in asset_ids:
        db.execute('INSERT INTO fanout_children(group_id,asset_id,state,created_at) '
                   'VALUES(?,?,?,?)', (group_id, asset_id, 'pending', now))


def attach_task(db, group_id, asset_id, task_id):
    db.execute('UPDATE fanout_children SET task_id=?,state=? WHERE group_id=? AND asset_id=?',
               (task_id, 'dispatched', group_id, asset_id))


def pending_children(db, group_id):
    """Children whose result has not arrived. Drives both completion and the
    'still running' answer, so the two can never disagree."""
    return db.execute("SELECT * FROM fanout_children WHERE group_id=? AND state IN "
                      "('pending','dispatched') ORDER BY asset_id", (group_id,)).fetchall()


def mark(db, group_id, asset_id, state, error_code=None):
    db.execute('UPDATE fanout_children SET state=?,error_code=? WHERE group_id=? AND asset_id=?',
               (state, error_code, group_id, asset_id))


def finish(db, group_id):
    db.execute('UPDATE fanout_groups SET state=?,updated_at=? WHERE id=?',
               ('done', time.time(), group_id))


def child_outcome(task_row):
    """Classify one child task for the aligned answer.

    The distinctions matter more than the text. "unreachable", "timed out" and
    "read it, it was empty" must never be collapsed: a model that sees 30 empty
    results will conclude thirty services are down, when in fact thirty hosts
    were unreachable. Reported separately so the model reasons about the right
    thing.
    """
    if task_row is None:
        return 'no_result', None, ''
    if task_row['state'] == 'unknown':
        return 'unknown', 'EXECUTION_UNKNOWN', ''
    if task_row['state'] in ('queued', 'awaiting_approval', 'claimed'):
        return 'running', None, ''
    if task_row['state'] == 'cancelled':
        return 'cancelled', None, ''
    try:
        result = json.loads(task_row['result']) if task_row['result'] else {}
    except ValueError:
        return 'malformed', 'RESULT_UNREADABLE', ''
    code = result.get('error_code')
    status = result.get('status')
    # Not clipped here: clipping is the assembler's job, because only it knows
    # how much of the shared budget is left. Trimming twice would also hide the
    # fact that a host's output was truncated.
    text = result.get('stdout') or ''
    if status == 'succeeded':
        return 'ok', None, text
    # Turn the Agent's error code into the category the model can act on.
    if code and ('TIMEOUT' in code or 'TIMED_OUT' in code):
        return 'timeout', code, text
    if code and ('CANNOT' in code or 'UNREACHABLE' in code or 'EXEC_FAILED' in code
                 or 'NOT_FOUND' in code or 'PROGRAM_MISSING' in code):
        return 'unreachable', code, text
    return 'failed', code or 'UNKNOWN_FAILURE', text


def assemble(group, children, task_lookup, include_text=True):
    """One aligned answer for the model.

    Shape is fixed and summarised first, then the per-host text is included
    under a shared byte budget. Both halves matter: without the summary the
    model mis-reads a wall of blobs, and without the text it cannot see
    anything. When the budget runs out the remaining hosts are listed by name
    with a count, never silently dropped.
    """
    outcomes, failures, rows = {}, [], []
    budget = TOTAL_TEXT_CHARS if include_text else 0
    omitted = 0
    for child in children:
        # A child that was already marked (timeout, cancelled) is taken at its
        # marked value. Re-deriving from the task row would turn a deliberate
        # "we gave up waiting" back into "no result", which reads to a model as
        # "this host returned nothing" -- a different and wrong conclusion.
        state = child['state'] if 'state' in child.keys() else None
        if state == 'timeout':
            kind, code, text = 'timeout', child['error_code'] or 'TIMEOUT', ''
        elif state == 'cancelled':
            kind, code, text = 'cancelled', None, ''
        elif state in ('pending', 'dispatched'):
            kind, code, text = 'running', None, ''
        elif state == 'ok':
            # Reported as good by the result hook. Still look up the text, but
            # do not let a missing task row demote a settled child to
            # "no_result": the outcome is already known, only the bytes are absent.
            kind, code, text = child_outcome(task_lookup(child['task_id']))
            if kind == 'no_result':
                kind = 'ok'
        elif state == 'failed':
            kind, code, text = child_outcome(task_lookup(child['task_id']))
            if kind == 'ok':
                # The hook recorded a failure; trust it over a task row that may
                # have been overwritten since.
                kind = 'failed'
        else:
            kind, code, text = child_outcome(task_lookup(child['task_id']))
        outcomes.setdefault(kind, []).append(child['asset_id'])
        if kind != 'ok':
            failures.append({'asset_id': child['asset_id'],
                             'status': 'running' if kind == 'running' else
                                       'timeout' if kind == 'timeout' else
                                       'unreachable' if kind == 'unreachable' else 'failed',
                             'error_code': code})
            continue
        if budget <= 0:
            omitted += 1
            continue
        clipped = text[:min(budget, PER_ASSET_CHARS)]
        # Say when a single host was clipped, so "the log ended there" and "we
        # stopped reading" are not confused.
        rows.append({'asset_id': child['asset_id'], 'text': clipped,
                     'truncated': len(text) > len(clipped)})
        budget -= len(clipped)
    answer = {
        'tool': group['tool'],
        'params': json.loads(group['params']),
        # Counted by outcome, never only "N succeeded": see child_outcome().
        'summary': {kind: len(assets) for kind, assets in sorted(outcomes.items())},
        'assets_total': len(children),
        'failures': failures,
        'results': rows,
        'note': ('Hosts are grouped by outcome. "unreachable" and "timeout" mean the '
                 'data was NOT read -- do not treat them as evidence about the service.'),
    }
    if omitted:
        answer['omitted_for_size'] = omitted
        answer['omitted_note'] = ('Results for %d hosts were omitted to fit the context. '
                                  'Read fewer hosts per call, or ask again for a subset.'
                                  % omitted)
    return answer
