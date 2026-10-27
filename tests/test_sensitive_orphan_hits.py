"""A sensitive hit must not outlive the asset it points at, visibly.

`purge` deletes every table that declares a foreign key onto `assets`, but
`sensitive_hits` deliberately has none: a hit is evidence and has to survive an
asset purge. The consequence is that a purged asset leaves hits behind, and an
unlock card for a missing asset can never be resolved -- unlocking re-reads the
path *through that asset*. Those hits must therefore be skipped when the inbox
is built, not shown as a dead card.

This was found on a real deployment: after purging the demo asset the inbox
still reported "sensitive hit, asset_name: null" with no way to clear it.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ai_ops import sensitive
from ai_ops.app import create_app

TOKEN = 't0ken-' + 'x' * 32

# assets has several NOT NULL columns; the values are irrelevant here, only the
# row's existence matters.
INSERT_ASSET = ("INSERT OR REPLACE INTO assets(id,name,allowed_users,notes,token_hash) "
                "VALUES(?,?,'[]','','h')")


@pytest.fixture
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), TOKEN, 'test-model')
    yield TestClient(app), app, tmp_path


def _admin():
    return {'Authorization': 'Bearer ' + TOKEN}


def test_hit_for_purged_asset_is_not_listed(env):
    client, app, tmp_path = env
    db = sqlite3.connect(tmp_path / 'control.db')
    db.row_factory = sqlite3.Row

    # Two assets; only one survives.
    for aid, name in (('live', 'Live'), ('gone', 'Gone')):
        db.execute(INSERT_ASSET, (aid, name))
    live_hit = sensitive.record(db, turn_id='t1', task_id='k1', asset_id='live',
                                path='/etc/live.conf', rule_ids=['r'], baseline_version='v')
    gone_hit = sensitive.record(db, turn_id='t1', task_id='k2', asset_id='gone',
                                path='/etc/gone.conf', rule_ids=['r'], baseline_version='v')
    db.execute("DELETE FROM assets WHERE id='gone'")
    db.commit()

    listed = [h['hit_id'] for h in sensitive.pending(db)]
    assert live_hit in listed
    assert gone_hit not in listed, 'inbox still shows an unlock card for a purged asset'

    # The API agrees with the helper the inbox is built from.
    inbox = client.get('/api/v1/inbox', headers=_admin()).json()
    ids = [h['hit_id'] for h in inbox['sensitive_hits']]
    assert live_hit in ids and gone_hit not in ids
