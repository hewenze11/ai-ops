"""Stable chronological chat pagination, isolated SQLite; no workers/model calls."""
from fastapi.testclient import TestClient
import pytest
from ai_ops.app import create_app
from ai_ops.turns import enqueue_turn

TOKEN = 'chat-pagination-fixture-not-production-123456'

@pytest.fixture
def history(tmp_path):
    app = create_app(str(tmp_path / 'history.db'), TOKEN, 'unused')
    c = TestClient(app, headers={'Authorization': 'Bearer ' + TOKEN})
    for role in ('ops', 'other'):
        c.post('/api/v1/roles', json={'id': role, 'name': role}).raise_for_status()
    with app.state.transaction() as db:
        for i in range(135):
            for role in ('ops', 'other'):
                tid = enqueue_turn(db, role, 'chat', f'{role}-{i}', [], 'confirm', f'{role}-{i}', {}, created_at=1700000000+i//3)
                db.execute("UPDATE role_turns SET state='completed' WHERE id=?", (tid,))
    yield c, app
    c.close()

def test_before_pages_stable_no_duplicates_and_role_isolation(history):
    c, app = history
    recent = c.get('/api/v1/roles/ops/turns?limit=100&latest=true').json()
    assert len(recent) == 100
    boundary = recent[0]['seq']
    with app.state.transaction() as db:
        enqueue_turn(db, 'ops', 'chat', 'new-arrival', [], 'confirm', 'new', {})
    older = c.get(f'/api/v1/roles/ops/turns?limit=100&latest=true&before={boundary}').json()
    assert len(older) == 35
    all_turns = older + recent
    assert len({t['id'] for t in all_turns}) == 135
    assert all(t['role_id'] == 'ops' for t in all_turns)
    assert [t['seq'] for t in all_turns] == sorted(t['seq'] for t in all_turns)
    assert c.get(f"/api/v1/roles/ops/turns?latest=true&before={older[0]['seq']}").json() == []
    assert c.get('/api/v1/roles/ops/turns?limit=100&latest=true').json()[-1]['prompt'] == 'new'

def test_after_before_filters_and_auth(history):
    c, _ = history
    rows = c.get('/api/v1/roles/ops/turns?after=20&before=31&limit=100').json()
    assert rows and all(20 < t['seq'] < 31 for t in rows)
    assert c.get('/api/v1/roles/ops/turns?before=10&after=10').status_code == 422
    assert c.get('/api/v1/roles/ops/turns?before=0').status_code == 422
    assert c.get('/api/v1/roles/ops/turns?before=-1').status_code == 422
    assert c.get('/api/v1/roles/ops/turns?before=50', headers={'Authorization': 'Bearer wrong'}).status_code == 403
