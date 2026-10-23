"""Last-mile account/mode revalidation shared by all transports."""
import json
import time

# How long an operator's reveal may sit queued before it is considered stale.
# The unlock window is 15 minutes; a reveal that has not run by then would hand
# back bytes for a question nobody is asking any more.
REVEAL_MAX_AGE_SECONDS = 900


def claim_allowed(db, task):
    asset = db.execute('SELECT * FROM assets WHERE id=?', (task['asset_id'],)).fetchone()
    if not asset or asset['retired_at'] is not None:
        return False
    if asset['connection_type'] == 'local':
        local = db.execute('SELECT enabled FROM local_host WHERE asset_id=?', (asset['id'],)).fetchone()
        if not local or not local['enabled']:
            return False
    payload = json.loads(task['payload'])
    kind = payload.get('kind', 'shell')
    if kind == 'reveal':
        # A reveal has no parent turn by design (an operator dispatches it, not
        # the model), so the turn-based checks below cannot apply. It carries
        # the authority of the read it was cloned from instead, and that is
        # re-checked the only way that still makes sense: the account must
        # still be allowed on the asset, and the grant must still be fresh.
        from .account_grants import permits
        if payload.get('run_as') not in json.loads(asset['allowed_users']):
            return False
        if not permits(db, payload.get('source_turn_id'), task['asset_id'], payload.get('run_as')):
            # reveal tasks have turn_id NULL; the authority being re-checked is
            # the original read's, which the dispatcher copied into the payload.
            return False
        return time.time() - (task['created_at'] or 0) <= REVEAL_MAX_AGE_SECONDS
    parent = db.execute('SELECT mode,execution_users FROM role_turns WHERE id=?', (task['turn_id'],)).fetchone()
    from .account_grants import permits
    if not permits(db, task['turn_id'], task['asset_id'], payload.get('run_as')):
        return False
    # Readonly tasks are non-mutating and need no approval, but they still run
    # under an immutable mode: a readonly turn only ever claims readonly work.
    mode_ok = parent['mode'] == 'readonly' if kind == 'readonly' else parent['mode'] in ('confirm', 'direct')
    return bool(parent and mode_ok and
                payload.get('mode') == parent['mode'] and
                payload.get('run_as') in json.loads(parent['execution_users']) and
                payload.get('run_as') in json.loads(asset['allowed_users']))
