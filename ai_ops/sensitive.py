"""Sensitive-content unlock (docs §51.4).

The problem this solves
-----------------------
The masking layer (§48) is deliberately dumb: it masks secret shapes whether or
not the operator wants them masked. In an outage the operator may genuinely
need the model to see one value ("is the token in this config the old one?").
Today the only options are "stay blind" or "paste the value into chat", and the
second one puts the secret in the transcript forever.

What this adds
--------------
A **one-shot, human-initiated re-read** of the same file with masking disabled:

  1. The model's read runs normally and comes back masked. If the baseline
     matched anything, the turn learns only ``sensitive_hit`` (which rule ids,
     which path, how many) -- never the value.
  2. The operator sees an unlock card in the inbox. Only the operator can act
     on it; there is no model-facing tool that triggers an unlock, because an
     AI that can unlock its own blindfold is not blindfolded (§41).
  3. Unlocking re-runs the *same* readonly task with ``reveal=True``. The
     result goes to the operator's audit view and explicitly NOT into the
     model's context.
  4. The grant expires immediately after that single use.

Hard limits (deliberate, from §51.4)
------------------------------------
* Read only. An unlock never authorises a write, an egress call, or a new
  account. It is a second read of bytes this turn was already allowed to read.
* Not persistent. There is no "unlock this file" toggle; each unlock is one
  result.
* Audited. who / when / which rule / which path, on both the request and the
  reveal.
* The path must be inside what the turn already read. An unlock cannot be used
  to read a *different* file that happens to be more interesting: the caller
  passes the hit id, and the path comes from the stored hit, not the model.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

# Unlocks are short-lived by design: an operator opens the card, reads the
# value, done. A long window turns a one-shot into a standing exemption.
UNLOCK_TTL_SECONDS = 900
MAX_REVEAL_CHARS = 20000
# How many bytes of unmasked output are held after a reveal. Bigger than
# MAX_REVEAL_CHARS on purpose: the response is truncated for display, the
# stored copy is what was actually returned.
MAX_STASH = 262144

SCHEMA = """
CREATE TABLE IF NOT EXISTS sensitive_hits(
 id TEXT PRIMARY KEY,
 turn_id TEXT,
 task_id TEXT,
 asset_id TEXT,
 path TEXT,
 rule_ids TEXT NOT NULL,
 hits INTEGER NOT NULL DEFAULT 1,
 baseline_version TEXT,
 mode TEXT,
 reveal_state TEXT NOT NULL DEFAULT 'masked',
 reveal_token TEXT,
 revealed_at REAL,
 revealed_bytes INTEGER,
 actor TEXT,
 created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sensitive_hits_turn ON sensitive_hits(turn_id);
CREATE INDEX IF NOT EXISTS sensitive_hits_state ON sensitive_hits(reveal_state);
-- Unmasked reveal output. Kept out of tasks.result on purpose: that column is
-- what becomes model-visible tool output, and a reveal must never go there.
-- One row per task, overwritten if the same task somehow reports twice.
CREATE TABLE IF NOT EXISTS reveal_secret(
 task_id TEXT PRIMARY KEY,
 hit_id TEXT,
 stdout TEXT NOT NULL DEFAULT '',
 stderr TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL);
"""


def migrate(db):
    db.executescript(SCHEMA)


def _baseline_version():
    """Which masking rules were in force when a hit was recorded.

    Recorded with every hit so "which rules were active then" stays answerable
    after a rule update (§45.2 item 6).
    """
    from .baseline import BASELINE_VERSION
    return BASELINE_VERSION


class UnlockConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    # §51.4 limit 3: typing the file name back is the cheap version of "are you
    # sure". It is not authentication -- it is a speed bump against a stray
    # click on a card that exposes secrets.
    confirm_path: str = ''
    reason: str = ''


def record(db, *, turn_id, task_id, asset_id, path, rule_ids, baseline_version):
    """Persist one masked hit and return the id the operator card will carry.

    Called when a readonly result is stored. Only rule ids and the path are
    kept: the value never reaches this table, so an unlock cannot be satisfied
    from the database alone -- it must re-run the read on the target.
    """
    hit_id = 'hit-' + secrets.token_hex(8)
    db.execute('INSERT INTO sensitive_hits(id,turn_id,task_id,asset_id,path,rule_ids,hits,'
               'baseline_version,mode,reveal_state,created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (hit_id, turn_id, task_id, asset_id, path or '',
                json.dumps(sorted(set(rule_ids))), 1, baseline_version,
                'readonly', 'masked', time.time()))
    return hit_id


def pending(db, turn_id=None):
    """Masked hits the operator has not unlocked yet (newest first)."""
    if turn_id:
        rows = db.execute("SELECT * FROM sensitive_hits WHERE reveal_state='masked' AND turn_id=? "
                          "ORDER BY created_at DESC", (turn_id,))
    else:
        rows = db.execute("SELECT * FROM sensitive_hits WHERE reveal_state='masked' "
                          "ORDER BY created_at DESC LIMIT 100")
    out = []
    for row in rows:
        asset = db.execute('SELECT name FROM assets WHERE id=?', (row['asset_id'],)).fetchone()
        out.append({'kind': 'sensitive_hit', 'hit_id': row['id'], 'turn_id': row['turn_id'],
                    'task_id': row['task_id'], 'asset_id': row['asset_id'],
                    'asset_name': asset['name'] if asset else None,
                    'path': row['path'], 'rule_ids': json.loads(row['rule_ids']),
                    'baseline_version': row['baseline_version'],
                    'created_at': row['created_at'],
                    # Say plainly what unlocking does and does not do.
                    'unlock_scope': 'one re-read of this same path, returned to the operator only',
                    'expires_in_seconds': UNLOCK_TTL_SECONDS})
    return out


def stash_reveal(db, *, task_id, hit_id, stdout='', stderr=''):
    """Park unmasked reveal output where the model cannot reach it.

    Deliberately a separate table rather than a column on ``tasks``: every
    reader of a task result (the model's tool-output builder, the audit view,
    the console) walks ``tasks``, and a secret sitting in that row would leak
    the first time someone added a field to a view. A separate table has to be
    joined on purpose, which is the property we want.
    """
    db.execute('INSERT INTO reveal_secret(task_id,hit_id,stdout,stderr,created_at) '
               'VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET '
               'hit_id=excluded.hit_id,stdout=excluded.stdout,stderr=excluded.stderr,created_at=excluded.created_at',
               (task_id, hit_id, stdout, stderr, time.time()))


def model_notice(rule_ids, path):
    """What the model is told instead of the value.

    The model gets a structured fact, not the secret: it can say "this file
    contains a private key; ask the operator if you need the value", which is
    the correct behaviour, and it cannot leak what it never received.
    """
    return {'sensitive_content_masked': True, 'rule_ids': sorted(set(rule_ids or [])),
            'path': path,
            'note': 'Masked as potentially secret. The operator can unlock it for their '
                    'own view; you will not receive the value.'}


def install(app, transaction, audit, admin):
    @app.get('/api/v1/sensitive-hits', dependencies=[Depends(admin)])
    def list_hits(turn_id: str | None = None):
        with transaction(write=False) as db:
            return {'items': pending(db, turn_id)}

    @app.post('/api/v1/sensitive-hits/{hit_id}/unlock', dependencies=[Depends(admin)])
    def unlock(hit_id: str, body: UnlockConfirmation | None = None):
        """Mark one hit as unlocked for the operator and hand back a token.

        This endpoint does NOT return the secret. It authorises a single
        re-read; the caller then fetches the value through
        ``GET /api/v1/sensitive-hits/{hit_id}/revealed``. Two steps on purpose:
        the confirmation and the read are separate audited actions, so a
        mis-click shows up in the log as an unlock with no reveal.
        """
        from .baseline import BASELINE_VERSION
        body = body or UnlockConfirmation()
        with transaction() as db:
            row = db.execute('SELECT * FROM sensitive_hits WHERE id=?', (hit_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Sensitive hit not found')
            if row['reveal_state'] != 'masked':
                raise HTTPException(409, 'Hit already handled')
            if body.confirm_path and body.confirm_path != row['path']:
                # A wrong confirmation is a signal, not a typo to forgive: the
                # operator thinks they are unlocking something else.
                audit(db, 'sensitive.unlock_refused', hit_id, 'admin',
                      {'reason': 'CONFIRM_PATH_MISMATCH', 'path': row['path']})
                raise HTTPException(422, 'Confirmation does not match the masked path')
            token = secrets.token_urlsafe(24)
            db.execute("UPDATE sensitive_hits SET reveal_state='unlocked',reveal_token=?,"
                       "revealed_at=?,actor=? WHERE id=?",
                       (token, time.time(), 'admin', hit_id))
            audit(db, 'sensitive.unlocked', hit_id, 'admin',
                  {'turn_id': row['turn_id'], 'path': row['path'],
                   'rule_ids': json.loads(row['rule_ids']),
                   'baseline_version': BASELINE_VERSION,
                   'expires_in_seconds': UNLOCK_TTL_SECONDS,
                   'reason': body.reason[:500],
                   'scope': 'operator view only; model context unchanged'})
            return {'hit_id': hit_id, 'reveal_token': token,
                    'expires_at': time.time() + UNLOCK_TTL_SECONDS,
                    'note': 'Fetch once with the token; the value is not stored. '
                            'The model does not receive it.'}

    @app.get('/api/v1/sensitive-hits/{hit_id}/revealed', dependencies=[Depends(admin)])
    def revealed(hit_id: str, token: str = ''):
        """Return the unmasked text for one unlocked hit, then burn the grant.

        Two states, because the control plane cannot read the host itself:

        * The reveal task already completed -> return the stashed bytes.
        * It has not been dispatched yet -> dispatch it and answer 202. The
          operator polls the same URL with the same token; the token survives
          until the bytes are actually handed over, because the *grant* is what
          one use consumes, not the request.

        The value never touches ``tasks.result``: it is parked in
        ``reveal_secret`` by the result endpoint so the model cannot reach it.
        """
        with transaction() as db:
            row = db.execute('SELECT * FROM sensitive_hits WHERE id=?', (hit_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Sensitive hit not found')
            if row['reveal_state'] not in ('unlocked', 'revealing'):
                raise HTTPException(409, 'Hit is not unlocked')
            if not secrets.compare_digest(row['reveal_token'] or '', token or ''):
                audit(db, 'sensitive.reveal_refused', hit_id, 'admin',
                      {'reason': 'TOKEN_MISMATCH'})
                raise HTTPException(403, 'Reveal token invalid')
            if time.time() - (row['revealed_at'] or 0) > UNLOCK_TTL_SECONDS:
                db.execute("UPDATE sensitive_hits SET reveal_state='expired',reveal_token=NULL "
                           "WHERE id=?", (hit_id,))
                audit(db, 'sensitive.unlock_expired', hit_id, 'admin', {})
                raise HTTPException(410, 'Unlock window expired; unlock again')

            task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            if task is None:
                raise HTTPException(409, 'The read this hit came from is gone')

            # Fresh dispatch: queue one reveal task cloned from the original
            # read, then answer 202. The clone keeps the same tool/params/args/account,
            # so the Agent revalidates it against the same whitelist as the read
            # that produced the hit -- a reveal cannot widen what is readable.
            if row['reveal_state'] == 'unlocked':
                payload = json.loads(task['payload']) if task['payload'] else {}
                if payload.get('kind') != 'readonly':
                    raise HTTPException(409, 'Only a file read can be revealed')
                reveal_id = str(uuid.uuid4())
                clone = {k: v for k, v in payload.items()
                         if k in ('tool', 'params', 'run_as', 'asset_id',
                                  'execution_users', 'timeout_seconds', 'role_id', 'mode',
                                  'execution_grants', 'authorization_revision')}
                # Rebuild argv from params instead of inheriting it. A clone that
                # copies a possibly-stale or missing `args` would ship an
                # unrunnable task and fail at the host, far from the cause.
                from .readonly_tools import build as build_argv, ReadonlyRejected
                try:
                    program, argv = build_argv(payload.get('tool'), payload.get('params') or {})
                except ReadonlyRejected as error:
                    raise HTTPException(409, 'The original read can no longer be expanded: %s' % error)
                clone['program'], clone['args'] = program, argv
                clone.update({'kind': 'reveal', 'hit_id': hit_id, 'source_turn_id': task['turn_id'],
                              'idempotency_key': 'reveal:' + hit_id})
                db.execute('INSERT INTO tasks(id,role_id,asset_id,payload,state,idempotency_key,'
                           'created_at,updated_at,turn_id) VALUES(?,?,?,?,?,?,?,?,?)',
                           (reveal_id, task['role_id'], task['asset_id'],
                            json.dumps(clone, ensure_ascii=False), 'queued',
                            clone['idempotency_key'], time.time(), time.time(), None))
                db.execute("UPDATE sensitive_hits SET reveal_state='revealing' WHERE id=?", (hit_id,))
                audit(db, 'sensitive.reveal_dispatched', hit_id, 'admin',
                      {'task_id': reveal_id, 'path': row['path'],
                       'rule_ids': json.loads(row['rule_ids']),
                       'scope': 'operator view only; model context unchanged'})
                return {'hit_id': hit_id, 'path': row['path'], 'revealed': False,
                        'pending': True, 'task_id': reveal_id,
                        'note': 'Re-read dispatched to the host. Poll this URL with the same '
                                'token; the grant is spent when the bytes are returned.'}

            stash = db.execute('SELECT * FROM reveal_secret WHERE hit_id=? ORDER BY created_at DESC',
                               (hit_id,)).fetchone()
            if stash is None:
                # Dispatched but no result yet -- still pending, not an error.
                return {'hit_id': hit_id, 'path': row['path'], 'revealed': False,
                        'pending': True,
                        'note': 'Waiting for the host to re-read the file.'}
            # One use only: burn the token and drop the stashed plaintext.
            db.execute("UPDATE sensitive_hits SET reveal_state='revealed',reveal_token=NULL "
                       "WHERE id=?", (hit_id,))
            db.execute('DELETE FROM reveal_secret WHERE hit_id=?', (hit_id,))
            audit(db, 'sensitive.revealed', hit_id, 'admin',
                  {'path': row['path'], 'rule_ids': json.loads(row['rule_ids']),
                   'bytes': len(stash['stdout'] or '')})
            return {'hit_id': hit_id, 'path': row['path'], 'revealed': True,
                    'text': (stash['stdout'] or '')[:MAX_REVEAL_CHARS],
                    'truncated': len(stash['stdout'] or '') > MAX_REVEAL_CHARS,
                    'note': 'Grant spent; the stored copy is now deleted.'}
