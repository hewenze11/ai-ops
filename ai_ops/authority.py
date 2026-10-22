"""Last-mile account/mode revalidation shared by all transports."""
import json


def claim_allowed(db, task):
    asset = db.execute('SELECT * FROM assets WHERE id=?', (task['asset_id'],)).fetchone()
    if not asset or asset['retired_at'] is not None:
        return False
    if asset['connection_type'] == 'local':
        local = db.execute('SELECT enabled FROM local_host WHERE asset_id=?', (asset['id'],)).fetchone()
        if not local or not local['enabled']:
            return False
    parent = db.execute('SELECT mode,execution_users FROM role_turns WHERE id=?', (task['turn_id'],)).fetchone()
    payload = json.loads(task['payload'])
    from .account_grants import permits
    if not permits(db, task['turn_id'], task['asset_id'], payload.get('run_as')):
        return False
    kind = payload.get('kind', 'shell')
    # Readonly tasks are non-mutating and need no approval, but they still run
    # under an immutable mode: a readonly turn only ever claims readonly work.
    mode_ok = parent['mode'] == 'readonly' if kind == 'readonly' else parent['mode'] in ('confirm', 'direct')
    return bool(parent and mode_ok and
                payload.get('mode') == parent['mode'] and
                payload.get('run_as') in json.loads(parent['execution_users']) and
                payload.get('run_as') in json.loads(asset['allowed_users']))
