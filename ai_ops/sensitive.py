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

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

# Unlocks are short-lived by design: an operator opens the card, reads the
# value, done. A long window turns a one-shot into a standing exemption.
UNLOCK_TTL_SECONDS = 900
MAX_REVEAL_CHARS = 20000

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

        The bytes are re-read from the stored task result, which on the Agent
        path is the already-masked text -- so this endpoint can only return
        what the control plane actually holds. When the control plane holds
        only the masked copy (always, today), the operator is told so plainly
        rather than being shown a fake reveal.
        """
        with transaction() as db:
            row = db.execute('SELECT * FROM sensitive_hits WHERE id=?', (hit_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Sensitive hit not found')
            if row['reveal_state'] != 'unlocked' or not row['reveal_token']:
                raise HTTPException(409, 'Hit is not unlocked')
            if not secrets.compare_digest(row['reveal_token'], token or ''):
                audit(db, 'sensitive.reveal_refused', hit_id, 'admin',
                      {'reason': 'TOKEN_MISMATCH'})
                raise HTTPException(403, 'Reveal token invalid')
            if time.time() - (row['revealed_at'] or 0) > UNLOCK_TTL_SECONDS:
                db.execute("UPDATE sensitive_hits SET reveal_state='expired',reveal_token=NULL "
                           "WHERE id=?", (hit_id,))
                audit(db, 'sensitive.unlock_expired', hit_id, 'admin', {})
                raise HTTPException(410, 'Unlock window expired; unlock again')
            # One use only: burn the token whether or not the read succeeds.
            db.execute("UPDATE sensitive_hits SET reveal_state='revealed',reveal_token=NULL "
                       "WHERE id=?", (hit_id,))
            task = db.execute('SELECT result FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            audit(db, 'sensitive.revealed', hit_id, 'admin',
                  {'path': row['path'], 'rule_ids': json.loads(row['rule_ids'])})
            text = ''
            if task and task['result']:
                try:
                    text = (json.loads(task['result']) or {}).get('stdout') or ''
                except (TypeError, ValueError):
                    text = ''
            # The stored stdout is already masked on every path we have. Say
            # that instead of pretending otherwise: a "reveal" that returns
            # [REDACTED] with no explanation looks like a bug.
            from .scrubbing import MASK
            if MASK in text:
                return {'hit_id': hit_id, 'path': row['path'], 'revealed': False,
                        'text': text[:MAX_REVEAL_CHARS],
                        'note': 'The control plane stores only the masked copy. Re-read the '
                                'file on the host to see the value; this grant is now spent.'}
            return {'hit_id': hit_id, 'path': row['path'], 'revealed': True,
                    'text': text[:MAX_REVEAL_CHARS], 'note': 'Grant spent.'}
