"""Asset notes use the same live source for editing, core docs and model context."""
import json
from fastapi.testclient import TestClient
from ai_ops.app import create_app
from ai_ops.context import build_messages
from ai_ops.scrubbing import redact_text

TOKEN = 'inventory-notes-test-admin-000000000000'

def test_notes_live_core_document_and_model(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), TOKEN)
    c = TestClient(app, headers={'Authorization': 'Bearer ' + TOKEN})
    aid = c.post('/api/v1/assets', json={'name': '测试服务器', 'notes': 'old', 'allowed_users': ['reader']}).json()['asset_id']
    path = '/api/v1/documents/system-asset-inventory'
    for note in ['第一行：测试环境\n第二行：不可删除业务', 'updated', '', 'token=supersecret-test-value-1234567890']:
        assert c.put('/api/v1/assets/' + aid + '/notes', json={'notes': note}).status_code == 200
        result = c.get(path)
        assert result.status_code == 200
        doc = result.json()
        assert doc['read_only'] and doc['derived']
        assets = {a['id']: a for a in doc['assets']}
        content = {a['id']: a for a in json.loads(doc['content'])}
        assert assets[aid]['notes'] == content[aid]['notes'] == redact_text(note)
        assert {'token_hash', 'previous_token_hash', 'credential_ref', 'private_key'} .isdisjoint(assets[aid])
        with app.state.transaction() as db:
            from ai_ops.observability import inventory_assets
            assert next(a for a in inventory_assets(db) if a['id'] == aid)['notes'] == redact_text(note)
        filtered = c.get(path, params={'execution_users': 'other'}).json()
        assert aid not in {a['id'] for a in filtered['assets']}
    assert TestClient(app).get(path).status_code == 401
    assert c.post('/api/v1/assets/' + aid + '/retire', json={'note': 'test removal'}).status_code == 200
    assert aid not in {a['id'] for a in c.get(path).json()['assets']}
